"""
Décisions de PV -- version API (remplace api2_pv_decision.py)
-------------------------------------------------------------
Route : POST /api/pv/decision
Corps JSON : {"pv_path": "...", "decision": "...", "motif": "...", "commentaire": "..."}

Le dashboard envoie directement la décision au serveur Flask (Render).
Le PV HTML est lu en SFTP, le commentaire est ajouté / remplacé / supprimé
sous la ligne "Point :", puis le PV est réécrit sur le serveur.
"""

import re
import html
import posixpath
import logging
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


def register_pv_decision(app, get_sftp_client, pv_base_dir):
    """Enregistre la route sur l'app Flask existante."""

    @app.route("/api/pv/decision", methods=["POST"])
    def pv_decision():
        data = request.get_json(silent=True) or {}
        pv_path = (data.get("pv_path") or "").strip()
        commentaire = (data.get("commentaire") or "").strip()

        if not pv_path:
            return jsonify({"erreur": "pv_path manquant"}), 400

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

            updated = insert_after_point(original, commentaire)
            if updated == original:
                return jsonify({"statut": "inchange"}), 200

            tmp_path = pv_path + ".tmp"
            with sftp.open(tmp_path, "wb") as f:
                f.write(updated.encode("utf-8"))
            try:
                sftp.posix_rename(tmp_path, pv_path)
            except Exception:
                sftp.remove(pv_path)
                sftp.rename(tmp_path, pv_path)

            log.info(f"PV mis à jour : {pv_path}")
            return jsonify({"statut": "ok"}), 200

        except FileNotFoundError:
            return jsonify({"erreur": f"PV introuvable : {pv_path}"}), 404
        except Exception as e:
            log.error(f"Erreur mise à jour PV : {e}")
            return jsonify({"erreur": str(e)}), 500
        finally:
            for obj in (sftp, transport):
                try:
                    if obj:
                        obj.close()
                except Exception:
                    pass
