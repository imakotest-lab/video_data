"""
API 2 -- traitement des décisions de PV
----------------------------------------
Tourne sur le PC (là où le dashboard télécharge les .txt).

Fonctionnement :
 1. Surveille le dossier Téléchargements.
 2. Quand un .txt de décision apparaît (1ère ligne = #PV_DECISION) :
      - lit decision / motif / commentaire / pv_path
      - télécharge le PV HTML depuis le serveur (SFTP)
      - ajoute/remplace/supprime le commentaire sous la ligne "Point :" du HTML
      - envoie le nouveau PV sur le serveur et remplace l'ancien
 3. Range le .txt traité dans Téléchargements/pv_traites/

Dépendance : pip install paramiko
Lancement  : python api2_pv_decision.py
"""

import os
import re
import time
import html
import shutil
import logging
import paramiko

# ---------------- CONFIG ----------------
SFTP_HOST = "160.154.40.6"
SFTP_PORT = 1222
SFTP_USER = "cs-collector"
SFTP_PASSWORD = "1m@ko"          # idéalement : os.environ["SFTP_PASSWORD"]

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
DONE_DIR = os.path.join(DOWNLOAD_DIR, "pv_traites")
ERROR_DIR = os.path.join(DOWNLOAD_DIR, "pv_erreurs")
POLL_SECONDS = 3
MARKER = "#PV_DECISION"

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger("api2")


# ---------------- LECTURE DU TXT ----------------
def parse_decision_txt(path):
    """Retourne un dict {decision, motif, commentaire, pv_path} ou None si ce n'est pas un txt de décision."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    if not lines or lines[0].strip() != MARKER:
        return None
    data = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            data[k.strip().lower()] = v.strip()
    return data


# ---------------- INSERTION DANS LE HTML ----------------
def build_comment_text(data):
    """Seul le commentaire saisi est inséré (ni décision, ni motif)."""
    return data.get("commentaire", "").strip()


COMMENT_RE = re.compile(
    r'<div class="pv-commentaire"[^>]*>.*?</div>(?=\s*(?:<|$))', re.IGNORECASE | re.DOTALL
)


def insert_after_point(html_text, text):
    """
    Met à jour le commentaire du PV (un seul bloc, sous la ligne 'Point :').
      - bloc déjà présent + nouveau texte  -> remplacé
      - bloc déjà présent + texte vide     -> supprimé
      - pas de bloc + nouveau texte        -> inséré juste après le </dl> de 'Point :'
      - pas de bloc + texte vide           -> PV inchangé
    """
    text = (text or "").strip()

    # 1) retirer l'ancien bloc s'il existe (le commentaire précédent est remplacé)
    had_block = bool(COMMENT_RE.search(html_text))
    cleaned = COMMENT_RE.sub("", html_text, count=1) if had_block else html_text

    if not text:
        return cleaned  # supprimé si présent, sinon inchangé

    safe = html.escape(text)
    block = (
        '<div class="pv-commentaire" style="margin-top:12px;">'
        '<span style="font-weight:600;">Commentaire :</span> '
        '<span style="font-family:\'Courier New\',Consolas,monospace;font-size:12.5px;">'
        + safe + '</span></div>'
    )

    # 2) insérer le nouveau bloc sous 'Point :' (après le </dl> de ce bloc)
    m = re.search(r"<dt>\s*Point\s*:\s*</dt>.*?</dl>", cleaned, re.IGNORECASE | re.DOTALL)
    if m:
        return cleaned[:m.end()] + block + cleaned[m.end():]

    log.warning("Bloc 'Point :' introuvable, ajout avant </body>")
    if re.search(r"</body>", cleaned, re.IGNORECASE):
        return re.sub(r"</body>", lambda _: block + "</body>", cleaned, count=1, flags=re.IGNORECASE)
    return cleaned + block


# ---------------- SFTP ----------------
def sftp_connect():
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)
    return paramiko.SFTPClient.from_transport(transport), transport


def process_pv(data):
    pv_path = data.get("pv_path", "")
    if not pv_path:
        raise ValueError("pv_path manquant dans le txt")
    if not pv_path.lower().endswith((".html", ".htm")):
        raise ValueError(f"Format non géré ({pv_path}) : seuls les PV HTML sont modifiables")

    sftp, transport = sftp_connect()
    try:
        with sftp.open(pv_path, "rb") as f:
            original = f.read().decode("utf-8", errors="replace")

        comment = build_comment_text(data)
        updated = insert_after_point(original, comment)
        if updated == original:
            log.info("Rien à modifier dans le PV (commentaire vide et aucun commentaire existant)")
            return

        tmp_path = pv_path + ".tmp"
        with sftp.open(tmp_path, "wb") as f:
            f.write(updated.encode("utf-8"))

        # Remplace l'ancien PV par le nouveau (l'ancien est supprimé)
        try:
            sftp.posix_rename(tmp_path, pv_path)
        except Exception:
            sftp.remove(pv_path)
            sftp.rename(tmp_path, pv_path)

        log.info(f"PV mis à jour sur le serveur : {pv_path}")
    finally:
        try:
            sftp.close()
        finally:
            transport.close()


# ---------------- SURVEILLANCE ----------------
def is_stable(path):
    try:
        s1 = os.path.getsize(path)
        time.sleep(0.5)
        return s1 == os.path.getsize(path)
    except OSError:
        return False


def move_to(folder, path):
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, os.path.basename(path))
    if os.path.exists(dest):
        base, ext = os.path.splitext(dest)
        dest = f"{base}_{int(time.time())}{ext}"
    shutil.move(path, dest)


def scan_once():
    for name in os.listdir(DOWNLOAD_DIR):
        if not name.lower().endswith(".txt"):
            continue
        path = os.path.join(DOWNLOAD_DIR, name)
        if not os.path.isfile(path) or not is_stable(path):
            continue
        try:
            data = parse_decision_txt(path)
        except Exception as e:
            log.error(f"Lecture impossible {name}: {e}")
            continue
        if data is None:
            continue  # txt ordinaire, on ignore

        log.info(f"Décision détectée : {name}")
        try:
            process_pv(data)
            move_to(DONE_DIR, path)
        except Exception as e:
            log.error(f"Échec traitement {name}: {e}")
            move_to(ERROR_DIR, path)


if __name__ == "__main__":
    log.info(f"Surveillance de {DOWNLOAD_DIR} (Ctrl+C pour arrêter)")
    while True:
        try:
            scan_once()
        except Exception as e:
            log.error(f"Erreur boucle: {e}")
        time.sleep(POLL_SECONDS)