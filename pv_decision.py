"""
Décisions de PV -- version API
-------------------------------------------------------------
POST /api/pv/decision : enregistre la décision (fichier .decision.json)
                        et le commentaire (dans le PV HTML)
POST /api/pv/cloture  : clôture un PV accepté
"""
import json
import re
import html
import posixpath
import logging
from datetime import datetime
from flask import jsonify, request

log = logging.getLogger("pv_decision")

COMMENT_RE = re.compile(
    r'<div class="pv-commentaire"[^>]*>.*?</div>(?=\s*(?:<|$))', re.IGNORECASE | re.DOTALL
)


def insert_after_point(html_text, text):
    """
    - bloc déjà présent + nouveau texte  -> remplacé
    - bloc déjà présent + texte vide     -> supprimé
    - pas de bloc + nouveau texte        -> inséré après le </dl> de 'Point :'
    - pas de bloc + texte vide           -> PV inchangé
    """
    text = (text or "").strip()

    had_block = bool(COMMENT_RE.search(html_text))
    cleaned = COMMENT_RE.sub("", html_text, count=1) if had_block else html_text

    if not text:
        return cleaned

    safe = html.escape(text)
    block = (
        '<div class="pv-commentaire" style="margin-top:12px;">'
        '<span style="font-weight:600;">Commentaire :</span> '
        '<span style="font-family:\'Courier New\',Consolas,monospace;font-size:12.5px;">'
        + safe + '</span></div>'
    )

    m = re.search(r"<dt>\s*Point\s*:\s*</dt>.*?</dl>", cleaned, re.IGNORECASE | re.DOTALL)
    if m:
        return cleaned[:m.end()] + block + cleaned[m.end():]

    log.warning("Bloc 'Point :' introuvable, ajout avant </body>")
    if re.search(r"</body>", cleaned, re.IGNORECASE):
        return re.sub(r"</body>", lambda _: block + "</body>", cleaned, count=1, flags=re.IGNORECASE)
    return cleaned + block


def read_decision(sftp, pv_path):
    try:
        with sftp.open(pv_path + ".decision.json", "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (IOError, ValueError):
        return None


def write_file(sftp, path, content_bytes):
    tmp = path + ".tmp"
    with sftp.open(tmp, "wb") as f:
        f.write(content_bytes)
    try:
        sftp.posix_rename(tmp, path)
    except Exception:
        sftp.remove(path)
        sftp.rename(tmp, path)


def _close(*objs):
    for obj in objs:
        try:
            if obj:
                obj.close()
        except Exception:
            pass


def register_pv_decision(app, get_sftp_client, pv_base_dir):
    """Enregistre les routes sur l'app Flask existante."""

    @app.route("/api/pv/cloture", methods=["POST"])
    def pv_cloture():
        data = request.get_json(silent=True) or {}
        pv_path = posixpath.normpath((data.get("pv_path") or "").strip())
        if not pv_path.startswith(pv_base_dir.rstrip("/") + "/"):
            return jsonify({"erreur": "Chemin non autorisé"}), 403
        sftp = transport = None
        try:
            sftp, transport = get_sftp_client()
            d = read_decision(sftp, pv_path)
            if not d or d.get("decision") != "accepte":
                return jsonify({"erreur": "Seul un PV accepté peut être clôturé"}), 400
            d["cloture"] = True
            d["date_cloture"] = datetime.now().isoformat(timespec="seconds")
            write_file(sftp, pv_path + ".decision.json",
                       json.dumps(d, ensure_ascii=False).encode("utf-8"))
            return jsonify({"statut": "ok"}), 200
        except Exception as e:
            log.error(f"Erreur clôture PV : {e}")
            return jsonify({"erreur": str(e)}), 500
        finally:
            _close(sftp, transport)

    @app.route("/api/pv/decision", methods=["POST"])
    def pv_decision():
        data = request.get_json(silent=True) or {}
        pv_path = (data.get("pv_path") or "").strip()
        commentaire = (data.get("commentaire") or "").strip()
        decision = (data.get("decision") or "").strip()
        motif = (data.get("motif") or "").strip()

        if not pv_path:
            return jsonify({"erreur": "pv_path manquant"}), 400
        if decision not in ("accepte", "rejete"):
            return jsonify({"erreur": "decision invalide"}), 400

        # Sécurité : le chemin doit rester dans le dossier des PV
        pv_path = posixpath.normpath(pv_path)
        if not pv_path.startswith(pv_base_dir.rstrip("/") + "/"):
            return jsonify({"erreur": "Chemin non autorisé"}), 403

        sftp = transport = None
        try:
            sftp, transport = get_sftp_client()

            # Si le chemin reçu est un .docx / .pdf, on cherche le .html du même PV
            if not pv_path.lower().endswith((".html", ".htm")):
                base = pv_path.rsplit(".", 1)[0]
                trouve = None
                for ext in (".html", ".htm"):
                    try:
                        sftp.stat(base + ext)
                        trouve = base + ext
                        break
                    except FileNotFoundError:
                        continue
                if not trouve:
                    return jsonify({"erreur": "Aucune version HTML de ce PV : seuls les PV HTML sont modifiables"}), 400
                pv_path = trouve

            with sftp.open(pv_path, "rb") as f:
                original = f.read().decode("utf-8", errors="replace")

            # Refus si le PV est déjà clôturé
            existant = read_decision(sftp, pv_path)
            if existant and existant.get("cloture"):
                return jsonify({"erreur": "PV clôturé : modification interdite"}), 409

            # Sauvegarde de l'ancienne version
            write_file(sftp, pv_path + ".bak", original.encode("utf-8"))

            # Commentaire dans le HTML
            updated = insert_after_point(original, commentaire)
            if updated != original:
                write_file(sftp, pv_path, updated.encode("utf-8"))

            # Décision côté serveur
            info = {
                "decision": decision,
                "motif": motif,
                "commentaire": commentaire,
                "cloture": False,
                "date": datetime.now().isoformat(timespec="seconds"),
            }
            write_file(sftp, pv_path + ".decision.json",
                       json.dumps(info, ensure_ascii=False).encode("utf-8"))

            log.info(f"PV mis à jour : {pv_path}")
            return jsonify({"statut": "ok"}), 200

        except FileNotFoundError:
            return jsonify({"erreur": f"PV introuvable : {pv_path}"}), 404
        except Exception as e:
            log.error(f"Erreur mise à jour PV : {e}")
            return jsonify({"erreur": str(e)}), 500
        finally:
            _close(sftp, transport)
