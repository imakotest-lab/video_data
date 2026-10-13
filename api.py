"""
API Flask -- pont entre le dashboard web et le serveur SFTP
-------------------------------------------------------------
Structure des dossiers identifiée :
- General/videos/         : Videos normales (TID-*.mp4)
- General/audio/          : Audio normaux (TID-*.wav)
- General/gps/            : GPS normaux (TID-*.json)

- Events/videos/          : Infractions vidéo simples (TID-*-tag-*.mp4)
- Events/videos/IR/       : Infractions vidéo Routières (TID-*-tag-*IR.mp4)
- Events/videos/AN/       : Infractions vidéo Installation Anarchique (TID-*-tag-*AN.mp4)

- Events/gps/             : Infractions GPS simples (TID-*-tag-*.json)
- Events/gps/IR/          : Infractions GPS Routières (TID-*-tag-*IR.json)
- Events/gps/AN/          : Infractions GPS Installation Anarchique (TID-*-tag-*AN.json)
- Events/gps/OC/          : Infractions GPS Photo d'Infraction (TID-*-tag-*OC.json)

- Events/audio/Vocaux/    : Infractions audio simples (TID-*-tag-*.wav)
- Events/audio/Vocaux/IR/ : Infractions audio Routières (TID-*-tag-*IR.wav)
- Events/audio/Vocaux/AN/ : Infractions audio Installation Anarchique (TID-*-tag-*AN.wav)

- Events/infraction/photos/      : Photos infractions simples (TID-*-tag-*.jpg)
- Events/infraction/photos/IR/   : Photos infractions Routières (TID-*-tag-*IR.jpg)
- Events/infraction/photos/AN/   : Photos infractions Installation Anarchique (TID-*-tag-*AN.jpg)
- Events/infraction/photos/OC/   : Photos d'Infraction (TID-*-tag-*OC.jpg / TID-DATEHEURE.jpg)

- Events/pv/               : PV infractions simples (TID-*-tag-*.docx)
- Events/pv/IR/            : PV infractions Routières (TID-*-tag-*IR.docx)
- Events/pv/AN/            : PV infractions Installation Anarchique (TID-*-tag-*AN.docx)
- Events/pv/OC/            : PV Photo d'Infraction (TID-*-tag-*OC.docx)
"""

from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS
import paramiko
import stat
import os
import json
import math
from datetime import datetime
import urllib.parse
import logging
import subprocess
import threading
import signal
import re
import time
from typing import Dict, List, Optional

# Configuration des logs
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# --- Configuration SFTP ---
SFTP_HOST = "160.154.40.6"
SFTP_PORT = 1222
SFTP_USER = "cs-collector"
SFTP_PASSWORD = "1m@ko"

# --- Configuration Scripts ---
SCRIPTS_DIR = "/home/cs-collector/script/V2"
SCRIPT_TIMEOUT = 300  # 5 minutes timeout

# Mapping entre les noms attendus par le dashboard et les noms réels des fichiers
SCRIPT_MAPPING = {
    "sorter_v4.py": "sorter_v4.py",
    "sorter": "sorter_v4.py",
    "renamer.py": "renamer_V2.py",
    "renamer": "renamer_V2.py",
    "archive.py": "archiver.py",
    "archive": "archiver.py",
    "sender_v3.py": "sender_v3.py",
    "sender": "sender_v3.py"
}

# ==================== CHEMINS MIS À JOUR ====================

# --- VIDÉOS ---
DOSSIER_VIDEOS_NORMAL = "/home/cs-collector/General/videos"
DOSSIER_VIDEOS_INFRACTION = "/home/cs-collector/Events/videos"
DOSSIER_VIDEOS_INFRACTION_IR = "/home/cs-collector/Events/videos/IR"
DOSSIER_VIDEOS_INFRACTION_AN = "/home/cs-collector/Events/videos/AN"

# --- AUDIO ---
DOSSIER_AUDIO_NORMAL = "/home/cs-collector/General/audio"
DOSSIER_AUDIO_INFRACTION = "/home/cs-collector/Events/audio/vocaux"
DOSSIER_AUDIO_INFRACTION_IR = "/home/cs-collector/Events/audio/vocaux/IR"
DOSSIER_AUDIO_INFRACTION_AN = "/home/cs-collector/Events/audio/vocaux/AN"

# --- GPS ---
DOSSIER_GPS_NORMAL = "/home/cs-collector/General/gps"
DOSSIER_GPS_INFRACTION = "/home/cs-collector/Events/gps"
DOSSIER_GPS_INFRACTION_IR = "/home/cs-collector/Events/gps/IR"
DOSSIER_GPS_INFRACTION_AN = "/home/cs-collector/Events/gps/AN"
DOSSIER_GPS_INFRACTION_OC = "/home/cs-collector/Events/gps/OC"

# --- PHOTOS ---
DOSSIER_PHOTOS_INFRACTION = "/home/cs-collector/Events/infraction/photos"
DOSSIER_PHOTOS_INFRACTION_IR = "/home/cs-collector/Events/infraction/photos/IR"
DOSSIER_PHOTOS_INFRACTION_AN = "/home/cs-collector/Events/infraction/photos/AN"
DOSSIER_PHOTOS_INFRACTION_OC = "/home/cs-collector/Events/infraction/photos/OC"

# --- PV ---
DOSSIER_PV_INFRACTION = "/home/cs-collector/Events/pv"
DOSSIER_PV_INFRACTION_IR = "/home/cs-collector/Events/pv/IR"
DOSSIER_PV_INFRACTION_AN = "/home/cs-collector/Events/pv/AN"
DOSSIER_PV_INFRACTION_OC = "/home/cs-collector/Events/pv/OC"

# Extensions
EXTENSIONS_VIDEO = (".mp4", ".avi", ".mov", ".mkv")
EXTENSIONS_AUDIO = (".wav", ".mp3", ".aac", ".flac", ".ogg", ".m4a", ".wma")
EXTENSIONS_GPS = (".json",)
EXTENSIONS_PHOTOS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
EXTENSIONS_PV = (".html", ".htm", ".docx", ".pdf")  # .docx = format généré par pv_generator.py ; .pdf gardé pour compat descendante

# ==================== GESTIONNAIRE DE SCRIPTS (VERSION DISTANTE UNIQUEMENT) ====================

class ScriptManager:
    """Gestionnaire des scripts exécutés à distance sur le serveur cs-collector via SSH."""

    def __init__(self):
        self.jobs: Dict[str, Dict] = {}
        self.lock = threading.Lock()
        self._ssh_client = None
        self._ssh_lock = threading.Lock()

    def _get_ssh_client(self) -> paramiko.SSHClient:
        """Obtient ou crée un client SSH persistant."""
        with self._ssh_lock:
            if self._ssh_client is None:
                self._ssh_client = self._ssh_connect()
            else:
                # Vérifier si la connexion est toujours active
                try:
                    transport = self._ssh_client.get_transport()
                    if transport is None or not transport.is_active():
                        self._ssh_client.close()
                        self._ssh_client = self._ssh_connect()
                except:
                    self._ssh_client = self._ssh_connect()
            return self._ssh_client

    def _ssh_connect(self) -> paramiko.SSHClient:
        """Établit une connexion SSH au serveur distant."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=SFTP_HOST,
            port=SFTP_PORT,
            username=SFTP_USER,
            password=SFTP_PASSWORD,
            timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        return client

    def _run_command(self, command: str, timeout: int = 30):
        """Exécute une commande sur le serveur distant avec connexion persistante."""
        client = self._get_ssh_client()
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_status = stdout.channel.recv_exit_status()
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            return exit_status, out, err
        except Exception as e:
            logger.error(f"Erreur execution commande: {e}")
            # Réinitialiser la connexion en cas d'erreur
            with self._ssh_lock:
                if self._ssh_client:
                    try:
                        self._ssh_client.close()
                    except:
                        pass
                    self._ssh_client = None
            raise

    def _sftp_read_file(self, log_path: str, max_lines: int = 1000) -> str:
        """Lit un fichier via SFTP avec connexion persistante."""
        client = self._get_ssh_client()
        try:
            sftp = client.open_sftp()
            try:
                with sftp.open(log_path, "r") as f:
                    lines = f.readlines()
                    if len(lines) > max_lines:
                        lines = lines[-max_lines:]
                    return ''.join(lines)
            except FileNotFoundError:
                return ""
            finally:
                sftp.close()
        except Exception as e:
            logger.error(f"Erreur lecture SFTP: {e}")
            return ""

    # ---------- Résolution du script ----------

    def get_script_path(self, script_name: str):
        """Résout le nom réel du script via SCRIPT_MAPPING (le fichier vit sur le serveur distant)."""
        real_name = SCRIPT_MAPPING.get(script_name, script_name)
        remote_path = f"{SCRIPTS_DIR}/{real_name}"
        return remote_path, real_name

    def _is_pid_alive(self, pid: int) -> bool:
        """Vérifie si un PID est actif sur le serveur distant."""
        try:
            exit_status, out, err = self._run_command(f"ps -p {pid} -o pid=")
            return out.strip() != ""
        except:
            return False

    # ---------- Démarrage ----------

    def start_script(self, script_name: str, script_id: str) -> Dict:
        """Démarre un script sur le serveur distant via SSH."""
        remote_path, real_script_name = self.get_script_path(script_name)

        # Vérifier si le script est déjà en cours
        with self.lock:
            job = self.jobs.get(script_id)
        
        if job:
            if job.get("status") == "running" and self._is_pid_alive(job.get("pid", 0)):
                return {
                    "status": "error",
                    "error": f"Le script {script_name} est déjà en cours d'exécution (PID {job['pid']})",
                }
            else:
                with self.lock:
                    if script_id in self.jobs:
                        del self.jobs[script_id]

        # FORCER la reconnexion en réinitialisant le client SSH
        with self._ssh_lock:
            if self._ssh_client:
                try:
                    self._ssh_client.close()
                except:
                    pass
                self._ssh_client = None

        # Maintenant la connexion est forcée à se reconnecter
        try:
            exit_status, out, err = self._run_command(f"test -f {remote_path} && echo OK || echo MISSING")
            if "OK" not in out:
                _, ls_out, _ = self._run_command(f"ls -la {SCRIPTS_DIR}")
                logger.info(f"Scripts disponibles dans {SCRIPTS_DIR}:\n{ls_out}")
                return {
                    "status": "error",
                    "error": f"Script {real_script_name} non trouvé dans {SCRIPTS_DIR}.",
                }
        except Exception as e:
            logger.error(f"Erreur de connexion lors de la vérification du script: {e}")
            return {
                "status": "error",
                "error": f"Erreur de connexion au serveur: {str(e)}",
            }

        log_path = f"/tmp/dashboard_{script_id}.log"
        
        try:
            self._run_command(f"rm -f {log_path}")
        except:
            pass

        launch_cmd = (
            f"cd {SCRIPTS_DIR} && "
            f"nohup python3 {real_script_name} > {log_path} 2>&1 < /dev/null & echo $!"
        )
        
        try:
            exit_status, out, err = self._run_command(launch_cmd)
        except Exception as e:
            return {
                "status": "error",
                "error": f"Erreur de lancement: {str(e)}",
            }
        
        pid_str = out.strip().splitlines()[-1] if out.strip() else ""

        if not pid_str.isdigit():
            return {
                "status": "error",
                "error": f"Impossible de récupérer le PID distant. stdout={out!r}",
            }

        pid = int(pid_str)

        with self.lock:
            self.jobs[script_id] = {
                "pid": pid,
                "log_path": log_path,
                "script_name": script_name,
                "real_script": real_script_name,
                "start_time": datetime.now().isoformat(),
                "end_time": None,
                "status": "running",
            }

        threading.Thread(target=self._watch_timeout, args=(script_id,), daemon=True).start()

        logger.info(f"Script {real_script_name} démarré à distance avec PID {pid}")

        return {
            "status": "success",
            "message": f"Script {real_script_name} démarré avec PID {pid}",
            "pid": pid,
        }
    # ---------- Surveillance / arrêt ----------

    def _watch_timeout(self, script_id: str):
        """Surveille le timeout du script et l'arrête si nécessaire."""
        time.sleep(SCRIPT_TIMEOUT)
        
        with self.lock:
            job = self.jobs.get(script_id)
        
        if job and job.get("status") == "running" and self._is_pid_alive(job["pid"]):
            logger.warning(f"Script {script_id} timeout après {SCRIPT_TIMEOUT}s, arrêt distant")
            self.stop_script(script_id)

    def stop_script(self, script_id: str) -> Dict:
        """Arrête un script en cours d'exécution sur le serveur distant."""
        with self.lock:
            job = self.jobs.get(script_id)
        
        if not job:
            # Essayer de trouver le script en cours via ps
            try:
                script_name_map = {
                    "sorter": "sorter_v4.py",
                    "renamer": "renamer_V2.py",
                    "archive": "archiver.py",
                    "sender": "sender_v3.py"
                }
                script_file = script_name_map.get(script_id, script_id)
                
                exit_status, out, err = self._run_command(
                    f"ps aux | grep '[p]ython3.*{script_file}' | grep -v grep | awk '{{print $2}}'"
                )
                pids = out.strip().split()
                
                if pids:
                    for pid in pids:
                        if pid.isdigit():
                            self._run_command(f"kill -TERM {pid} 2>/dev/null || true")
                            time.sleep(1)
                            self._run_command(f"kill -9 {pid} 2>/dev/null || true")
                    
                    # Supprimer le job s'il existe
                    with self.lock:
                        if script_id in self.jobs:
                            del self.jobs[script_id]
                    
                    return {"status": "stopped", "message": f"Script {script_id} arrêté avec succès (PID(s) {', '.join(pids)})"}
                else:
                    return {"status": "error", "error": f"Script {script_id} non trouvé en cours d'exécution"}
            except Exception as e:
                logger.error(f"Erreur lors de la recherche du PID pour {script_id}: {e}")
                return {"status": "error", "error": str(e)}

        pid = job["pid"]
        logger.info(f"Arrêt du script {script_id} avec PID {pid}")
        
        # Arrêter le processus
        self._run_command(f"kill -TERM -{pid} 2>/dev/null || true")
        time.sleep(2)
        self._run_command(f"pkill -TERM -P {pid} 2>/dev/null || true")
        time.sleep(1)
        
        if self._is_pid_alive(pid):
            self._run_command(f"kill -KILL -{pid} 2>/dev/null || true")
            self._run_command(f"pkill -KILL -P {pid} 2>/dev/null || true")
            self._run_command(f"kill -KILL {pid} 2>/dev/null || true")

        # SUPPRIMER le job au lieu de le garder en "stopped"
        with self.lock:
            if script_id in self.jobs:
                del self.jobs[script_id]

        return {"status": "stopped", "message": f"Script {script_id} arrêté avec succès (PID {pid})"}

    # ---------- Lecture du statut / sortie ----------

    def get_script_output(self, script_id: str) -> Dict:
        """Récupère la sortie d'un script."""
        with self.lock:
            job = self.jobs.get(script_id)

        if not job:
            return {
                "output": "", 
                "status": {"status": "idle"}, 
                "is_running": False
            }

        # Vérifier si le script est toujours en cours
        is_running = self._is_pid_alive(job["pid"])

        # Si le script était marqué running mais n'est plus actif, mettre à jour
        if not is_running and job.get("status") == "running":
            with self.lock:
                if script_id in self.jobs:
                    self.jobs[script_id]["status"] = "success"
                    self.jobs[script_id]["end_time"] = datetime.now().isoformat()
            job = self.jobs[script_id]

        # Lire le contenu du log - Utilise _sftp_read_file
        output = self._sftp_read_file(job["log_path"])

        return {
            "output": output,
            "status": job,
            "is_running": is_running,
        }


# Créer le gestionnaire de scripts (UNIQUE VERSION DISTANTE)
script_manager = ScriptManager()

# ==================== FONCTIONS UTILITAIRES ====================

def get_sftp_client():
    """Ouvre une connexion SFTP avec logs"""
    try:
        logger.info(f"Connexion a {SFTP_HOST}:{SFTP_PORT} avec {SFTP_USER}")
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)
        sftp = paramiko.SFTPClient.from_transport(transport)
        logger.info("Connexion SFTP reussie")
        return sftp, transport
    except Exception as e:
        logger.error(f"Erreur de connexion SFTP: {e}")
        raise

def compter_dossier(sftp, chemin, extensions=()):
    """Compte les fichiers dans un dossier"""
    try:
        logger.debug(f"Comptage des fichiers dans: {chemin}")
        entrees = sftp.listdir_attr(chemin)
        logger.debug(f"  Nombre d'entrees: {len(entrees)}")
    except FileNotFoundError:
        logger.warning(f"Dossier non trouve: {chemin}")
        return 0, 0, 0
    except Exception as e:
        logger.error(f"Erreur lors de la lecture de {chemin}: {e}")
        return 0, 0, 0
    
    fichiers = [e for e in entrees if not stat.S_ISDIR(e.st_mode)]
    taille = sum(f.st_size for f in fichiers)
    
    if extensions:
        nb_type = sum(1 for f in fichiers if f.filename.lower().endswith(extensions))
        logger.debug(f"  Fichiers avec extensions {extensions}: {nb_type}")
    else:
        nb_type = len(fichiers)
        logger.debug(f"  Total fichiers: {nb_type}")
    
    return len(fichiers), taille, nb_type

def compter_avec_sous_dossiers(sftp, base_path, extensions):
    """Compte les fichiers dans un dossier et ses sous-dossiers IR, AN et OC"""
    total = 0
    taille = 0
    
    # Compter dans le dossier racine
    nb, taille_base, _ = compter_dossier(sftp, base_path, extensions)
    total += nb
    taille += taille_base
    
    # Compter dans les sous-dossiers IR, AN et OC
    # (OC n'existe que pour photos/gps/pv ; pour les dossiers qui n'ont pas de sous-dossier OC,
    #  compter_dossier renvoie simplement 0 sans erreur -- voir gestion FileNotFoundError)
    for sub in ["IR", "AN", "OC"]:
        sub_path = f"{base_path}/{sub}"
        nb_sub, taille_sub, _ = compter_dossier(sftp, sub_path, extensions)
        total += nb_sub
        taille += taille_sub
    
    return total, taille

def extract_infraction_type(filename: str, subfolder: str = "") -> str:
    """
    Extrait le type d'infraction à partir du nom de fichier et du sous-dossier
    Retourne: "normal", "simple", "ir", "an", "oc"
    """
    name_without_ext = filename.rsplit('.', 1)[0]
    parts = name_without_ext.split('-')
    
    if len(parts) >= 3:
        time_str = parts[2]
        
        if len(time_str) > 4 and time_str[4] == '-':
            tag_part = time_str[5:]
            
            if tag_part.endswith('IR'):
                return "ir"
            elif tag_part.endswith('AN'):
                return "an"
            elif tag_part.endswith('OC'):
                return "oc"

    # Si le tag ne contient pas d'indicateur, vérifier le sous-dossier
    if subfolder == "IR":
        return "ir"
    elif subfolder == "AN":
        return "an"
    elif subfolder == "OC":
        return "oc"
    elif subfolder == "simple" or (subfolder and subfolder not in ["normal", "IR", "AN", "OC"]):
        return "simple"
    
    return "normal"

def extract_common_info(filename: str, subfolder: str = "") -> Dict:
    """
    Extrait les informations communes à tous les types de fichiers.
    Gère deux formats de nom :
      - TID-DATE-TIME[-tag-TAG].ext   (avec tag)
      - TID-DATEHEURE.ext             (12 chiffres collés, sans tag -- ex: photos OC)
    """
    result = {
        "filename": filename,
        "display_name": filename.rsplit('.', 1)[0],
        "date": "01/01/1970",
        "time": "00:00",
        "datetime": None,
        "tag": None,
        "is_infraction": False,
        "infraction_type": "normal"
    }
    
    name_without_ext = filename.rsplit('.', 1)[0]
    parts = name_without_ext.split('-')

    date_str = None
    time_str = None

    if len(parts) >= 3 and len(parts[1]) == 8 and parts[1].isdigit():
        # Format avec tag : TID-DATE-TIME[-tag-TAG]
        date_str = parts[1]
        time_str = parts[2]
    elif len(parts) >= 2 and len(parts[1]) == 12 and parts[1].isdigit():
        # Format sans tag : TID-DATEHEURE (12 chiffres collés)
        date_str = parts[1][:8]
        time_str = parts[1][8:12]

    if date_str is not None and time_str is not None:
        time_clean = time_str[:4] if len(time_str) >= 4 else "0000"
        
        tag = None
        is_infraction = False
        infraction_type = "normal"
        
        # Détection du tag (uniquement pertinent pour le format avec "-tag-")
        if len(time_str) > 4 and time_str[4] == '-':
            tag_part = time_str[5:]
            
            # Vérifier si le tag contient un indicateur IR ou AN
            if tag_part.endswith('IR'):
                tag = tag_part[:-2]
                is_infraction = True
                infraction_type = "ir"
            elif tag_part.endswith('AN'):
                tag = tag_part[:-2]
                is_infraction = True
                infraction_type = "an"
            elif tag_part:
                tag = tag_part
                is_infraction = True
                # Si dans un sous-dossier, on utilise le type du dossier
                if subfolder == "IR":
                    infraction_type = "ir"
                elif subfolder == "AN":
                    infraction_type = "an"
                elif subfolder == "OC":
                    infraction_type = "oc"
                else:
                    infraction_type = "simple"
        elif subfolder in ("IR", "AN", "OC"):
            # Pas de tag dans le nom (ex: TID-DATEHEURE.jpg) mais fichier situé
            # dans un sous-dossier d'infraction -> on hérite du type du dossier
            is_infraction = True
            infraction_type = subfolder.lower()
        
        # Extraire la date
        if len(date_str) == 8:
            year = date_str[:4]
            month = date_str[4:6]
            day = date_str[6:8]
            result["date"] = f"{day}/{month}/{year}"
            
            try:
                result["datetime"] = datetime(
                    int(year), int(month), int(day),
                    int(time_clean[:2]), int(time_clean[2:4])
                )
            except:
                pass
        
        # Extraire l'heure
        if len(time_clean) == 4:
            hour = time_clean[:2]
            minute = time_clean[2:4]
            result["time"] = f"{hour}:{minute}"
        
        result["tag"] = tag
        result["is_infraction"] = is_infraction
        result["infraction_type"] = infraction_type
    
    return result

def get_subcategory_label(infraction_type: str) -> Dict:
    """
    Retourne le libellé et la classe CSS pour un type d'infraction
    """
    labels = {
        "normal": {"label": "✓ Normal", "class": "tag-ok", "status": "Aucune anomalie"},
        "simple": {"label": "⚠ Infraction", "class": "tag-infraction", "status": "Infraction détectée"},
        "ir": {"label": "🚗 Infraction Routière", "class": "tag-infraction", "status": "Infraction Routière"},
        "an": {"label": "🏗️ Installation Anarchique", "class": "tag-infraction", "status": "Installation Anarchique"},
        "oc": {"label": "📷 Photo d'Infraction", "class": "tag-infraction", "status": "Photo d'Infraction"}
    }
    return labels.get(infraction_type, labels["normal"])

def list_files_generic(sftp, base_path: str, extensions: tuple, subfolder: str = "", max_files: int = None) -> List[Dict]:
    """
    Fonction générique pour lister les fichiers avec gestion des sous-dossiers
    """
    results = []
    
    try:
        logger.info(f"Lecture de {base_path}")
        files = sftp.listdir_attr(base_path)
        
        for entry in files:
            if not stat.S_ISDIR(entry.st_mode):
                filename = entry.filename
                if filename.lower().endswith(extensions):
                    info = extract_common_info(filename, subfolder)
                    infraction_type = info["infraction_type"]
                    
                    # Si le type est normal mais qu'on est dans un sous-dossier infraction
                    if infraction_type == "normal" and subfolder not in ["normal", ""]:
                        infraction_type = subfolder.lower()
                    
                    subcat = get_subcategory_label(infraction_type)
                    
                    full_path = f"{base_path}/{filename}"
                    
                    result = {
                        "filename": filename,
                        "path": base_path,
                        "full_path": full_path,
                        "type": "infraction" if info["is_infraction"] or infraction_type != "normal" else "normal",
                        "subcategory": infraction_type,
                        "size": entry.st_size,
                        "mtime": entry.st_mtime,
                        "date": info["date"],
                        "time": info["time"],
                        "date_display": f"{info['date']} - {info['time']}",
                        "tag": info.get("tag"),
                        "status": subcat["status"],
                        "tag_class": subcat["class"],
                        "tag_label": subcat["label"]
                    }
                    
                    results.append(result)
                    
                    if max_files and len(results) >= max_files:
                        return results
                    
    except FileNotFoundError:
        logger.warning(f"Dossier non trouvé: {base_path}")
    except Exception as e:
        logger.error(f"Erreur lecture {base_path}: {e}")
    
    return results

def lire_fichier_gps(sftp, chemin_fichier):
    """
    Lit un fichier GPS (un JSON par ligne) et retourne les points
    """
    points = []
    try:
        with sftp.open(chemin_fichier, 'r') as f:
            lignes = f.readlines()
        
        for ligne in lignes:
            ligne = ligne.strip()
            if ligne:
                try:
                    points.append(json.loads(ligne))
                except:
                    pass
    except Exception as e:
        logger.error(f"Erreur lecture GPS {chemin_fichier}: {e}")
    
    return points

def calculer_distance_km(points):
    """Calcule la distance totale en km avec la formule de Haversine"""
    if len(points) < 2:
        return 0
    
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371
        lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    distance = 0
    for i in range(len(points) - 1):
        p1, p2 = points[i], points[i+1]
        lat1, lon1 = p1.get('latitude'), p1.get('longitude')
        lat2, lon2 = p2.get('latitude'), p2.get('longitude')
        if lat1 and lon1 and lat2 and lon2:
            distance += haversine(lat1, lon1, lat2, lon2)
    
    return distance

def generer_lien_google_maps(points):
    """
    Genere un lien Google Maps avec tous les points du trajet
    """
    if not points:
        return None
    
    # Filtrer les points avec coordonnees valides
    coords = []
    for p in points:
        lat = p.get('latitude')
        lng = p.get('longitude')
        if lat is not None and lng is not None:
            coords.append(f"{lat},{lng}")
    
    if not coords:
        return None
    
    # Si un seul point, utiliser le format simple
    if len(coords) == 1:
        return f"https://www.google.com/maps?q={coords[0]}"
    
    # Si plusieurs points, utiliser le format itineraire
    return f"https://www.google.com/maps/dir/{'/'.join(coords)}"

# ==================== FONCTIONS POUR LES LOGS ====================

def parse_log_line(line: str, source: str = "") -> Dict:
    """
    Parse une ligne de log pour extraire le temps, le niveau et le message
    """
    patterns = [
        r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(\w+):\s*(.*)',
        r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+\[(\w+)\]\s*(.*)',
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*-\s*(\w+)\s*-\s*(.*)',
        r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(.*)',
    ]
    
    line = line.rstrip('\n')
    if not line:
        return None
    
    for pattern in patterns:
        match = re.match(pattern, line)
        if match:
            groups = match.groups()
            if len(groups) == 3:
                time_str, level, message = groups
                return {
                    "time": time_str,
                    "level": level.upper(),
                    "message": message.strip(),
                    "source": source
                }
            elif len(groups) == 2:
                time_str, message = groups
                level = detect_level_from_message(message)
                return {
                    "time": time_str,
                    "level": level,
                    "message": message.strip(),
                    "source": source
                }
    
    time_match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
    if time_match:
        time_str = time_match.group(1)
        remaining = line.replace(time_str, '').strip()
        level = detect_level_from_message(remaining)
        return {
            "time": time_str,
            "level": level,
            "message": remaining,
            "source": source
        }
    
    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": "INFO",
        "message": line,
        "source": source
    }

def detect_level_from_message(message: str) -> str:
    """
    Détecte le niveau de log à partir du message
    """
    msg_lower = message.lower()
    if 'error' in msg_lower or '❌' in msg_lower or 'erreur' in msg_lower:
        return 'ERROR'
    elif 'warning' in msg_lower or '⚠️' in msg_lower or 'attention' in msg_lower:
        return 'WARNING'
    elif 'success' in msg_lower or '✅' in msg_lower or 'succès' in msg_lower:
        return 'SUCCESS'
    elif 'debug' in msg_lower or '🐛' in msg_lower:
        return 'DEBUG'
    else:
        return 'INFO'

def read_log_file(sftp, filepath: str, max_lines: int = 500) -> list:
    """
    Lit un fichier de log et retourne les lignes parsées
    """
    logs = []
    try:
        with sftp.open(filepath, 'r') as f:
            lines = f.readlines()
        
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        
        source = os.path.basename(filepath)
        for line in lines:
            parsed = parse_log_line(line, source)
            if parsed:
                logs.append(parsed)
        
        return logs
    except Exception as e:
        logger.error(f"Erreur lecture log {filepath}: {e}")
        return []

def filter_logs(logs: list, level: str = None, search: str = None) -> list:
    """
    Filtre les logs par niveau et recherche textuelle
    """
    filtered = logs
    
    if level and level != 'all':
        filtered = [l for l in filtered if l.get('level') == level.upper()]
    
    if search:
        search_lower = search.lower()
        filtered = [l for l in filtered if search_lower in l.get('message', '').lower() or search_lower in l.get('time', '').lower()]
    
    return filtered

# ==================== ENDPOINTS VIDÉOS ====================

@app.route("/api/videos")
def list_videos():
    """Liste toutes les vidéos disponibles avec la nouvelle structure"""
    sftp = None
    transport = None
    
    try:
        logger.info("=" * 60)
        logger.info("DEBUT de l'endpoint /api/videos")
        logger.info("=" * 60)
        
        sftp, transport = get_sftp_client()
        videos = []
        
        # 1. Vidéos normales (General/videos)
        videos.extend(list_files_generic(
            sftp, DOSSIER_VIDEOS_NORMAL, EXTENSIONS_VIDEO, "normal"
        ))
        
        # 2. Infractions simples (Events/videos racine)
        videos.extend(list_files_generic(
            sftp, DOSSIER_VIDEOS_INFRACTION, EXTENSIONS_VIDEO, "simple"
        ))
        
        # 3. Infractions IR (Events/videos/IR)
        videos.extend(list_files_generic(
            sftp, DOSSIER_VIDEOS_INFRACTION_IR, EXTENSIONS_VIDEO, "IR"
        ))
        
        # 4. Infractions AN (Events/videos/AN)
        videos.extend(list_files_generic(
            sftp, DOSSIER_VIDEOS_INFRACTION_AN, EXTENSIONS_VIDEO, "AN"
        ))
        
        # Trier par date (plus récent en premier)
        videos.sort(key=lambda x: x["mtime"], reverse=True)
        
        logger.info("=" * 60)
        logger.info(f"FIN - Total vidéos retournées: {len(videos)}")
        logger.info("=" * 60)
        
        return jsonify({
            "videos": videos,
            "total": len(videos)
        })
        
    except Exception as e:
        logger.error(f"Erreur générale: {e}")
        return jsonify({"erreur": str(e)}), 500
    finally:
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass

# ==================== ENDPOINTS AUDIO ====================

@app.route("/api/audio")
def list_audio():
    """Liste tous les fichiers audio disponibles avec la nouvelle structure"""
    sftp = None
    transport = None
    
    try:
        logger.info("=" * 60)
        logger.info("DEBUT de l'endpoint /api/audio")
        logger.info("=" * 60)
        
        sftp, transport = get_sftp_client()
        audio_files = []
        
        # 1. Audio normaux (General/audio)
        audio_files.extend(list_files_generic(
            sftp, DOSSIER_AUDIO_NORMAL, EXTENSIONS_AUDIO, "normal"
        ))
        
        # 2. Infractions simples (Events/audio/Vocaux)
        audio_files.extend(list_files_generic(
            sftp, DOSSIER_AUDIO_INFRACTION, EXTENSIONS_AUDIO, "simple"
        ))
        
        # 3. Infractions IR (Events/audio/Vocaux/IR)
        audio_files.extend(list_files_generic(
            sftp, DOSSIER_AUDIO_INFRACTION_IR, EXTENSIONS_AUDIO, "IR"
        ))
        
        # 4. Infractions AN (Events/audio/Vocaux/AN)
        audio_files.extend(list_files_generic(
            sftp, DOSSIER_AUDIO_INFRACTION_AN, EXTENSIONS_AUDIO, "AN"
        ))
        
        # Ajouter une durée par défaut
        for audio in audio_files:
            audio["duration"] = "00:00"
            audio["agent"] = "TID"  # Valeur par défaut
        
        # Trier par date
        audio_files.sort(key=lambda x: x["mtime"], reverse=True)
        
        logger.info("=" * 60)
        logger.info(f"FIN - Total audio retournés: {len(audio_files)}")
        logger.info("=" * 60)
        
        return jsonify({
            "audio": audio_files,
            "total": len(audio_files)
        })
        
    except Exception as e:
        logger.error(f"Erreur générale audio: {e}")
        return jsonify({"erreur": str(e)}), 500
    finally:
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass

@app.route("/api/audio/stream/<path:filepath>")
def stream_audio(filepath):
    """Stream un fichier audio depuis le serveur SFTP"""
    sftp = None
    transport = None
    
    try:
        decoded_path = urllib.parse.unquote(filepath)
        if not decoded_path.startswith('/'):
            decoded_path = '/' + decoded_path
        
        logger.info(f"Streaming audio: {decoded_path}")
        
        sftp, transport = get_sftp_client()
        
        try:
            sftp.stat(decoded_path)
        except FileNotFoundError:
            return jsonify({"erreur": f"Fichier non trouvé: {decoded_path}"}), 404
        
        file_obj = sftp.open(decoded_path, 'rb')
        
        # Type MIME pour l'audio
        mime_type = 'audio/mpeg'
        if decoded_path.endswith('.mp3'):
            mime_type = 'audio/mpeg'
        elif decoded_path.endswith('.wav'):
            mime_type = 'audio/wav'
        elif decoded_path.endswith('.aac'):
            mime_type = 'audio/aac'
        elif decoded_path.endswith('.flac'):
            mime_type = 'audio/flac'
        elif decoded_path.endswith('.ogg'):
            mime_type = 'audio/ogg'
        elif decoded_path.endswith('.m4a'):
            mime_type = 'audio/mp4'
        elif decoded_path.endswith('.wma'):
            mime_type = 'audio/x-ms-wma'
        
        def generate():
            try:
                while True:
                    data = file_obj.read(8192)
                    if not data:
                        break
                    yield data
            finally:
                file_obj.close()
                sftp.close()
                transport.close()
        
        return Response(
            stream_with_context(generate()),
            mimetype=mime_type,
            headers={
                'Content-Disposition': f'inline; filename="{os.path.basename(decoded_path)}"',
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'no-cache'
            }
        )
        
    except Exception as e:
        logger.error(f"Erreur streaming audio: {e}")
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass
        return jsonify({"erreur": str(e)}), 500

# ==================== ENDPOINTS GPS ====================

@app.route("/api/gps")
def list_gps():
    """Liste tous les fichiers GPS disponibles avec la nouvelle structure"""
    sftp = None
    transport = None
    
    try:
        logger.info("=" * 60)
        logger.info("DEBUT de l'endpoint /api/gps")
        logger.info("=" * 60)
        
        sftp, transport = get_sftp_client()
        gps_files = []
        
        # 1. GPS normaux (General/gps)
        gps_files.extend(list_files_generic(
            sftp, DOSSIER_GPS_NORMAL, EXTENSIONS_GPS, "normal"
        ))
        
        # 2. Infractions simples (Events/gps racine)
        gps_files.extend(list_files_generic(
            sftp, DOSSIER_GPS_INFRACTION, EXTENSIONS_GPS, "simple"
        ))
        
        # 3. Infractions IR (Events/gps/IR)
        gps_files.extend(list_files_generic(
            sftp, DOSSIER_GPS_INFRACTION_IR, EXTENSIONS_GPS, "IR"
        ))
        
        # 4. Infractions AN (Events/gps/AN)
        gps_files.extend(list_files_generic(
            sftp, DOSSIER_GPS_INFRACTION_AN, EXTENSIONS_GPS, "AN"
        ))

        # 5. Infractions OC (Events/gps/OC)
        gps_files.extend(list_files_generic(
            sftp, DOSSIER_GPS_INFRACTION_OC, EXTENSIONS_GPS, "OC"
        ))
        
        # Enrichir avec les données GPS
        for gps in gps_files:
            try:
                points = lire_fichier_gps(sftp, gps["full_path"])
                distance_km = calculer_distance_km(points)
                vitesses = [p.get('speed', 0) for p in points if p.get('speed') is not None]
                
                gps["points"] = len(points)
                gps["distance_km"] = distance_km
                gps["distance_m"] = distance_km * 1000
                gps["vitesse_max"] = max(vitesses) if vitesses else 0
                gps["vitesse_moyenne"] = sum(vitesses) / len(vitesses) if vitesses else 0
                gps["google_maps_url"] = generer_lien_google_maps(points)
                gps["agent"] = "TID"  # Valeur par défaut
                gps["vehicle"] = gps["filename"].rsplit('.', 1)[0]
            except Exception as e:
                logger.warning(f"Erreur lecture données GPS {gps['filename']}: {e}")
                gps["points"] = 0
                gps["distance_km"] = 0
                gps["distance_m"] = 0
                gps["vitesse_max"] = 0
                gps["vitesse_moyenne"] = 0
        
        # Trier par date
        gps_files.sort(key=lambda x: x["mtime"], reverse=True)
        
        logger.info("=" * 60)
        logger.info(f"FIN - Total GPS retournés: {len(gps_files)}")
        logger.info("=" * 60)
        
        return jsonify({
            "gps": gps_files,
            "total": len(gps_files)
        })
        
    except Exception as e:
        logger.error(f"Erreur générale GPS: {e}")
        return jsonify({"erreur": str(e)}), 500
    finally:
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass

@app.route("/api/gps/data/<path:filepath>")
def gps_data(filepath):
    """Retourne les données GPS détaillées en JSON"""
    sftp = None
    transport = None
    
    try:
        decoded_path = urllib.parse.unquote(filepath)
        if not decoded_path.startswith('/'):
            decoded_path = '/' + decoded_path
        
        sftp, transport = get_sftp_client()
        
        # Lire les points GPS
        points = lire_fichier_gps(sftp, decoded_path)
        
        if not points:
            return jsonify({"error": "Aucun point GPS trouvé"}), 404
        
        # Statistiques
        vitesses = [p.get('speed', 0) for p in points if p.get('speed') is not None]
        latitudes = [p.get('latitude') for p in points if p.get('latitude') is not None]
        longitudes = [p.get('longitude') for p in points if p.get('longitude') is not None]
        altitudes = [p.get('altitude') for p in points if p.get('altitude') is not None]
        
        distance_km = calculer_distance_km(points)
        
        result = {
            "filename": os.path.basename(decoded_path),
            "points": points,
            "total_points": len(points),
            "distance_km": distance_km,
            "distance_m": distance_km * 1000,
            "vitesse_max": max(vitesses) if vitesses else 0,
            "vitesse_moyenne": sum(vitesses) / len(vitesses) if vitesses else 0,
            "vitesse_min": min(vitesses) if vitesses else 0,
            "altitude_max": max(altitudes) if altitudes else 0,
            "altitude_min": min(altitudes) if altitudes else 0,
            "debut": points[0].get('timestamp') if points else None,
            "fin": points[-1].get('timestamp') if points else None,
            "premier_point": {"lat": latitudes[0], "lng": longitudes[0]} if latitudes and longitudes else None,
            "dernier_point": {"lat": latitudes[-1], "lng": longitudes[-1]} if latitudes and longitudes else None,
            "google_maps_url": generer_lien_google_maps(points)
        }
        
        # Calculer la durée
        if result["debut"] and result["fin"]:
            try:
                debut = datetime.fromisoformat(result["debut"].replace(' ', 'T'))
                fin = datetime.fromisoformat(result["fin"].replace(' ', 'T'))
                result["duree_secondes"] = (fin - debut).total_seconds()
                result["duree"] = str(fin - debut)
            except:
                pass
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Erreur GPS data: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass

# ==================== ENDPOINTS PHOTOS ====================

@app.route("/api/photos")
def list_photos():
    """Liste toutes les photos disponibles avec la nouvelle structure"""
    sftp = None
    transport = None
    
    try:
        logger.info("=" * 60)
        logger.info("DEBUT de l'endpoint /api/photos")
        logger.info("=" * 60)
        
        sftp, transport = get_sftp_client()
        photos = []
        
        # 1. Photos infractions simples (Events/infraction/photos racine)
        photos.extend(list_files_generic(
            sftp, DOSSIER_PHOTOS_INFRACTION, EXTENSIONS_PHOTOS, "simple"
        ))
        
        # 2. Photos IR (Events/infraction/photos/IR)
        photos.extend(list_files_generic(
            sftp, DOSSIER_PHOTOS_INFRACTION_IR, EXTENSIONS_PHOTOS, "IR"
        ))
        
        # 3. Photos AN (Events/infraction/photos/AN)
        photos.extend(list_files_generic(
            sftp, DOSSIER_PHOTOS_INFRACTION_AN, EXTENSIONS_PHOTOS, "AN"
        ))

        # 4. Photos OC (Events/infraction/photos/OC)
        photos.extend(list_files_generic(
            sftp, DOSSIER_PHOTOS_INFRACTION_OC, EXTENSIONS_PHOTOS, "OC"
        ))
        
        # Trier par date
        photos.sort(key=lambda x: x["mtime"], reverse=True)
        
        logger.info("=" * 60)
        logger.info(f"FIN - Total photos retournées: {len(photos)}")
        logger.info("=" * 60)
        
        return jsonify({
            "photos": photos,
            "total": len(photos)
        })
        
    except Exception as e:
        logger.error(f"Erreur générale photos: {e}")
        return jsonify({"erreur": str(e)}), 500
    finally:
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass

@app.route("/api/photos/stream/<path:filepath>")
def stream_photo(filepath):
    """Stream une photo depuis le serveur SFTP"""
    sftp = None
    transport = None
    
    try:
        decoded_path = urllib.parse.unquote(filepath)
        if not decoded_path.startswith('/'):
            decoded_path = '/' + decoded_path
        
        logger.info(f"Streaming photo: {decoded_path}")
        
        sftp, transport = get_sftp_client()
        
        try:
            sftp.stat(decoded_path)
        except FileNotFoundError:
            return jsonify({"erreur": f"Fichier non trouvé: {decoded_path}"}), 404
        
        file_obj = sftp.open(decoded_path, 'rb')
        
        # Type MIME pour les photos
        mime_type = 'image/jpeg'
        if decoded_path.endswith('.png'):
            mime_type = 'image/png'
        elif decoded_path.endswith('.gif'):
            mime_type = 'image/gif'
        elif decoded_path.endswith('.bmp'):
            mime_type = 'image/bmp'
        elif decoded_path.endswith('.webp'):
            mime_type = 'image/webp'
        
        def generate():
            try:
                while True:
                    data = file_obj.read(8192)
                    if not data:
                        break
                    yield data
            finally:
                file_obj.close()
                sftp.close()
                transport.close()
        
        return Response(
            stream_with_context(generate()),
            mimetype=mime_type,
            headers={
                'Content-Disposition': f'inline; filename="{os.path.basename(decoded_path)}"',
                'Cache-Control': 'no-cache'
            }
        )
        
    except Exception as e:
        logger.error(f"Erreur streaming photo: {e}")
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass
        return jsonify({"erreur": str(e)}), 500

# ==================== ENDPOINTS PV ====================

@app.route("/api/pv")
def list_pv():
    """Liste tous les PV disponibles avec la nouvelle structure (AN, IR, OC, simples)"""
    sftp = None
    transport = None
    
    try:
        logger.info("=" * 60)
        logger.info("DEBUT de l'endpoint /api/pv")
        logger.info("=" * 60)
        
        sftp, transport = get_sftp_client()
        pv_files = []
        
        # 1. PV simples (Events/pv racine)
        pv_files.extend(list_files_generic(
            sftp, DOSSIER_PV_INFRACTION, EXTENSIONS_PV, "simple"
        ))
        
        # 2. PV IR (Events/pv/IR)
        pv_files.extend(list_files_generic(
            sftp, DOSSIER_PV_INFRACTION_IR, EXTENSIONS_PV, "IR"
        ))
        
        # 3. PV AN (Events/pv/AN)
        pv_files.extend(list_files_generic(
            sftp, DOSSIER_PV_INFRACTION_AN, EXTENSIONS_PV, "AN"
        ))

        # 4. PV OC (Events/pv/OC)
        pv_files.extend(list_files_generic(
            sftp, DOSSIER_PV_INFRACTION_OC, EXTENSIONS_PV, "OC"
        ))
        
        # Ajouter des informations spécifiques aux PV
        for pv in pv_files:
            suffixe = pv["subcategory"].upper() if pv["subcategory"] in ("an", "ir", "oc") else ""
            base_numero = f"PV-{pv['date'].replace('/', '')}-{pv['time'].replace(':', '')}"
            pv["numero_pv"] = f"{base_numero}-{suffixe}" if suffixe else base_numero
            pv["statut"] = "En attente"
            pv["format"] = pv["filename"].rsplit('.', 1)[-1].lower()  # "docx" ou "pdf"
        
        # Trier par date
        pv_files.sort(key=lambda x: x["mtime"], reverse=True)
        
        logger.info("=" * 60)
        logger.info(f"FIN - Total PV retournés: {len(pv_files)}")
        logger.info("=" * 60)
        
        return jsonify({
            "pv": pv_files,
            "total": len(pv_files)
        })
        
    except Exception as e:
        logger.error(f"Erreur générale PV: {e}")
        return jsonify({"erreur": str(e)}), 500
    finally:
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass

@app.route("/api/pv/stream/<path:filepath>")
def stream_pv(filepath):
    """Stream un PV (.docx ou .pdf) depuis le serveur SFTP"""
    sftp = None
    transport = None
    
    try:
        decoded_path = urllib.parse.unquote(filepath)
        if not decoded_path.startswith('/'):
            decoded_path = '/' + decoded_path
        
        logger.info(f"Streaming PV: {decoded_path}")
        
        sftp, transport = get_sftp_client()
        
        try:
            sftp.stat(decoded_path)
        except FileNotFoundError:
            return jsonify({"erreur": f"Fichier non trouvé: {decoded_path}"}), 404
        
        file_obj = sftp.open(decoded_path, 'rb')

               # Mimetype dynamique selon l'extension réelle du fichier
        lower_path = decoded_path.lower()
        if lower_path.endswith(('.html', '.htm')):
            mime_type = 'text/html'   # Flask ajoute charset=utf-8 automatiquement
        elif lower_path.endswith('.docx'):
            mime_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        elif lower_path.endswith('.pdf'):
            mime_type = 'application/pdf'
        else:
            mime_type = 'application/octet-stream'
        
        def generate():
            try:
                while True:
                    data = file_obj.read(8192)
                    if not data:
                        break
                    yield data
            finally:
                file_obj.close()
                sftp.close()
                transport.close()
        
        return Response(
            stream_with_context(generate()),
            mimetype=mime_type,
            headers={
                'Content-Disposition': f'inline; filename="{os.path.basename(decoded_path)}"',
                'Cache-Control': 'no-cache'
            }
        )
        
    except Exception as e:
        logger.error(f"Erreur streaming PV: {e}")
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass
        return jsonify({"erreur": str(e)}), 500

# ==================== ENDPOINTS STATS ET STREAM ====================

@app.route("/api/stats")
def stats():
    """Endpoint de statistiques avec tous les types d'infractions (IR, AN, OC)"""
    sftp = None
    transport = None
    
    try:
        sftp, transport = get_sftp_client()
        
        # --- Statistiques vidéos ---
        nb_videos_normal, taille_videos_normal, _ = compter_dossier(
            sftp, DOSSIER_VIDEOS_NORMAL, EXTENSIONS_VIDEO
        )
        nb_videos_infraction, taille_videos_infraction = compter_avec_sous_dossiers(
            sftp, DOSSIER_VIDEOS_INFRACTION, EXTENSIONS_VIDEO
        )
        
        # Détail des infractions vidéo
        nb_videos_simple, _, _ = compter_dossier(sftp, DOSSIER_VIDEOS_INFRACTION, EXTENSIONS_VIDEO)
        nb_videos_ir, _, _ = compter_dossier(sftp, DOSSIER_VIDEOS_INFRACTION_IR, EXTENSIONS_VIDEO)
        nb_videos_an, _, _ = compter_dossier(sftp, DOSSIER_VIDEOS_INFRACTION_AN, EXTENSIONS_VIDEO)
        
        # --- Statistiques audio ---
        nb_audio_normal, taille_audio_normal, _ = compter_dossier(
            sftp, DOSSIER_AUDIO_NORMAL, EXTENSIONS_AUDIO
        )
        nb_audio_infraction, taille_audio_infraction = compter_avec_sous_dossiers(
            sftp, DOSSIER_AUDIO_INFRACTION, EXTENSIONS_AUDIO
        )
        
        # Détail des infractions audio
        nb_audio_simple, _, _ = compter_dossier(sftp, DOSSIER_AUDIO_INFRACTION, EXTENSIONS_AUDIO)
        nb_audio_ir, _, _ = compter_dossier(sftp, DOSSIER_AUDIO_INFRACTION_IR, EXTENSIONS_AUDIO)
        nb_audio_an, _, _ = compter_dossier(sftp, DOSSIER_AUDIO_INFRACTION_AN, EXTENSIONS_AUDIO)
        
        # --- Statistiques GPS ---
        nb_gps_normal, taille_gps_normal, _ = compter_dossier(
            sftp, DOSSIER_GPS_NORMAL, EXTENSIONS_GPS
        )
        nb_gps_infraction, taille_gps_infraction = compter_avec_sous_dossiers(
            sftp, DOSSIER_GPS_INFRACTION, EXTENSIONS_GPS
        )
        
        # Détail des infractions GPS
        nb_gps_simple, _, _ = compter_dossier(sftp, DOSSIER_GPS_INFRACTION, EXTENSIONS_GPS)
        nb_gps_ir, _, _ = compter_dossier(sftp, DOSSIER_GPS_INFRACTION_IR, EXTENSIONS_GPS)
        nb_gps_an, _, _ = compter_dossier(sftp, DOSSIER_GPS_INFRACTION_AN, EXTENSIONS_GPS)
        nb_gps_oc, _, _ = compter_dossier(sftp, DOSSIER_GPS_INFRACTION_OC, EXTENSIONS_GPS)
        
        # --- Statistiques photos ---
        nb_photos_total, taille_photos = compter_avec_sous_dossiers(
            sftp, DOSSIER_PHOTOS_INFRACTION, EXTENSIONS_PHOTOS
        )
        nb_photos_simple, _, _ = compter_dossier(sftp, DOSSIER_PHOTOS_INFRACTION, EXTENSIONS_PHOTOS)
        nb_photos_ir, _, _ = compter_dossier(sftp, DOSSIER_PHOTOS_INFRACTION_IR, EXTENSIONS_PHOTOS)
        nb_photos_an, _, _ = compter_dossier(sftp, DOSSIER_PHOTOS_INFRACTION_AN, EXTENSIONS_PHOTOS)
        nb_photos_oc, _, _ = compter_dossier(sftp, DOSSIER_PHOTOS_INFRACTION_OC, EXTENSIONS_PHOTOS)
                
        
        # --- Statistiques PV ---
        nb_pv_total, taille_pv = compter_avec_sous_dossiers(
            sftp, DOSSIER_PV_INFRACTION, EXTENSIONS_PV
        )
        nb_pv_simple, _, _ = compter_dossier(sftp, DOSSIER_PV_INFRACTION, EXTENSIONS_PV)
        nb_pv_ir, _, _ = compter_dossier(sftp, DOSSIER_PV_INFRACTION_IR, EXTENSIONS_PV)
        nb_pv_an, _, _ = compter_dossier(sftp, DOSSIER_PV_INFRACTION_AN, EXTENSIONS_PV)
        nb_pv_oc, _, _ = compter_dossier(sftp, DOSSIER_PV_INFRACTION_OC, EXTENSIONS_PV)
        
        # --- Totaux ---
        total_video = nb_videos_normal + nb_videos_infraction
        total_audio = nb_audio_normal + nb_audio_infraction
        total_gps = nb_gps_normal + nb_gps_infraction
        
        taille_totale = (taille_videos_normal + taille_videos_infraction +
                        taille_audio_normal + taille_audio_infraction +
                        taille_gps_normal + taille_gps_infraction +
                        taille_photos + taille_pv)
        taille_go = round(taille_totale / (1024 ** 3), 2)
        
        return jsonify({
            # Vidéos
            "video_normal": nb_videos_normal,
            "infractions_video_simple": nb_videos_simple,
            "infractions_video_ir": nb_videos_ir,
            "infractions_video_an": nb_videos_an,
            "infractions_video": nb_videos_infraction,
            "total_video": total_video,
            
            # Audio
            "audio_normal": nb_audio_normal,
            "infractions_audio_simple": nb_audio_simple,
            "infractions_audio_ir": nb_audio_ir,
            "infractions_audio_an": nb_audio_an,
            "infractions_audio": nb_audio_infraction,
            "total_audio": total_audio,
            
            # GPS
            "gps_normal": nb_gps_normal,
            "infractions_gps_simple": nb_gps_simple,
            "infractions_gps_ir": nb_gps_ir,
            "infractions_gps_an": nb_gps_an,
            "infractions_gps_oc": nb_gps_oc,
            "infractions_gps": nb_gps_infraction,
            "total_gps": total_gps,
            
            # Photos
            "total_photos": nb_photos_total,
            "photos_simple": nb_photos_simple,
            "photos_ir": nb_photos_ir,
            "photos_an": nb_photos_an,
            "photos_oc": nb_photos_oc,
            
            # PV
            "total_pv": nb_pv_total,
            "pv_simple": nb_pv_simple,
            "pv_ir": nb_pv_ir,
            "pv_an": nb_pv_an,
            "pv_oc": nb_pv_oc,
            
            # Taille
            "taille_go": taille_go,
            "taille_octets": taille_totale,
            "derniere_analyse": datetime.now().isoformat(),
            "status": "success"
        })
        
    except Exception as e:
        logger.error(f"Erreur stats: {e}")
        return jsonify({"erreur": str(e), "status": "error"}), 500
    finally:
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass

@app.route("/api/video/stream/<path:filepath>")
def stream_video(filepath):
    """Stream une vidéo depuis le serveur SFTP"""
    sftp = None
    transport = None
    
    try:
        decoded_path = urllib.parse.unquote(filepath)
        if not decoded_path.startswith('/'):
            decoded_path = '/' + decoded_path
        
        logger.info(f"Streaming vidéo: {decoded_path}")
        
        sftp, transport = get_sftp_client()
        
        try:
            sftp.stat(decoded_path)
        except FileNotFoundError:
            return jsonify({"erreur": f"Fichier non trouvé: {decoded_path}"}), 404
        
        file_obj = sftp.open(decoded_path, 'rb')
        
        mime_type = 'video/mp4'
        if decoded_path.endswith('.avi'):
            mime_type = 'video/x-msvideo'
        elif decoded_path.endswith('.mov'):
            mime_type = 'video/quicktime'
        elif decoded_path.endswith('.mkv'):
            mime_type = 'video/x-matroska'
        
        def generate():
            try:
                while True:
                    data = file_obj.read(8192)
                    if not data:
                        break
                    yield data
            finally:
                file_obj.close()
                sftp.close()
                transport.close()
        
        return Response(
            stream_with_context(generate()),
            mimetype=mime_type,
            headers={
                'Content-Disposition': f'inline; filename="{os.path.basename(decoded_path)}"',
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'no-cache'
            }
        )
        
    except Exception as e:
        logger.error(f"Erreur streaming vidéo: {e}")
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass
        return jsonify({"erreur": str(e)}), 500

@app.route("/api/video/check/<path:filepath>")
def check_video(filepath):
    """Endpoint de vérification pour voir si un fichier existe"""
    sftp = None
    transport = None
    
    try:
        decoded_path = urllib.parse.unquote(filepath)
        if not decoded_path.startswith('/'):
            decoded_path = '/' + decoded_path
        
        sftp, transport = get_sftp_client()
        
        try:
            stat_info = sftp.stat(decoded_path)
            return jsonify({
                "exists": True,
                "path": decoded_path,
                "size": stat_info.st_size,
                "mtime": datetime.fromtimestamp(stat_info.st_mtime).isoformat()
            })
        except FileNotFoundError:
            return jsonify({
                "exists": False,
                "path": decoded_path,
                "error": "Fichier non trouvé"
            }), 404
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass

# ==================== ENDPOINTS SCRIPTS ====================

@app.route("/api/execute/script", methods=['POST'])
def execute_script():
    """
    Exécute un script Python en arrière-plan sur le serveur distant
    Attend : {"script": "sort.py", "script_id": "sort"}
    """
    try:
        data = request.json
        if not data:
            return jsonify({
                "status": "error",
                "error": "Données JSON requises"
            }), 400
        
        script_name = data.get('script')
        script_id = data.get('script_id')
        
        if not script_name:
            return jsonify({
                "status": "error",
                "error": "Le nom du script est requis"
            }), 400
        
        if not script_id:
            return jsonify({
                "status": "error",
                "error": "script_id est requis"
            }), 400
        
        logger.info(f"Demande d'exécution du script: {script_name} (id: {script_id})")
        
        result = script_manager.start_script(script_name, script_id)
        
        if result.get("status") == "error":
            return jsonify(result), 400
        
        return jsonify({
            "status": "success",
            "message": result.get("message"),
            "pid": result.get("pid")
        })
        
    except Exception as e:
        logger.error(f"Erreur lors de l'exécution du script: {e}")
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

@app.route("/api/execute/stop", methods=['POST'])
def stop_script():
    """
    Arrête un script en cours d'exécution sur le serveur distant
    Attend : {"script_id": "sort"}
    """
    try:
        data = request.json
        if not data:
            return jsonify({
                "status": "error",
                "error": "Données JSON requises"
            }), 400
        
        script_id = data.get('script_id')
        
        if not script_id:
            return jsonify({
                "status": "error",
                "error": "script_id est requis"
            }), 400
        
        logger.info(f"Demande d'arrêt du script: {script_id}")
        
        result = script_manager.stop_script(script_id)
        
        if result.get("status") == "error":
            return jsonify(result), 400
        
        return jsonify({
            "status": "stopped",
            "message": result.get("message")
        })
        
    except Exception as e:
        logger.error(f"Erreur lors de l'arrêt du script: {e}")
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

@app.route("/api/execute/status/<script_id>", methods=['GET'])
def script_status(script_id):
    """
    Récupère le statut d'un script sur le serveur distant
    """
    try:
        result = script_manager.get_script_output(script_id)
        
        return jsonify({
            "script_id": script_id,
            "status": result["status"],
            "is_running": result["is_running"],
            "output": result["output"]
        })
        
    except Exception as e:
        logger.error(f"Erreur lors de la récupération du statut: {e}")
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

# ==================== ENDPOINTS LOGS ====================

@app.route("/api/logs/<filename>")
def get_log_file(filename):
    """
    Récupère le contenu d'un fichier de log
    """
    try:
        allowed_logs = ['server_copier.log', 'server_handler.log', 'file_renamer.log']
        if filename not in allowed_logs:
            return jsonify({
                "error": f"Fichier de log non autorisé. Autorisés: {allowed_logs}"
            }), 400
        
        max_lines = request.args.get('lines', 100, type=int)
        level = request.args.get('level', 'all')
        search = request.args.get('search', '')
        
        if max_lines > 1000:
            max_lines = 1000
        if max_lines < 1:
            max_lines = 1
        
        log_path = f"/home/cs-collector/script/{filename}"
        
        sftp, transport = get_sftp_client()
        
        try:
            try:
                sftp.stat(log_path)
            except FileNotFoundError:
                sftp.close()
                transport.close()
                return jsonify({
                    "error": f"Fichier {filename} non trouvé sur le serveur",
                    "logs": [],
                    "total": 0,
                    "file": filename
                }), 404
            
            logs = read_log_file(sftp, log_path, max_lines)
            
            if level and level != 'all':
                logs = filter_logs(logs, level)
            if search:
                logs = filter_logs(logs, None, search)
            
            logs.sort(key=lambda x: x.get('time', ''), reverse=True)
            
            sftp.close()
            transport.close()
            
            return jsonify({
                "logs": logs,
                "total": len(logs),
                "file": filename,
                "params": {
                    "lines": max_lines,
                    "level": level,
                    "search": search
                }
            })
            
        except Exception as e:
            sftp.close()
            transport.close()
            logger.error(f"Erreur lecture log {filename}: {e}")
            return jsonify({
                "error": str(e),
                "logs": [],
                "total": 0,
                "file": filename
            }), 500
            
    except Exception as e:
        logger.error(f"Erreur endpoint logs: {e}")
        return jsonify({
            "error": str(e),
            "logs": [],
            "total": 0
        }), 500

@app.route("/api/logs/all")
def get_all_logs():
    """
    Récupère tous les logs fusionnés
    """
    try:
        max_lines = request.args.get('lines', 100, type=int)
        level = request.args.get('level', 'all')
        search = request.args.get('search', '')
        
        if max_lines > 500:
            max_lines = 500
        if max_lines < 1:
            max_lines = 1
        
        log_files = ['server_copier.log', 'server_handler.log', 'file_renamer.log']
        
        sftp, transport = get_sftp_client()
        
        all_logs = []
        
        for filename in log_files:
            log_path = f"/home/cs-collector/script/{filename}"
            try:
                sftp.stat(log_path)
                logs = read_log_file(sftp, log_path, max_lines)
                all_logs.extend(logs)
            except FileNotFoundError:
                logger.warning(f"Fichier {filename} non trouvé")
            except Exception as e:
                logger.error(f"Erreur lecture {filename}: {e}")
        
        sftp.close()
        transport.close()
        
        if level and level != 'all':
            all_logs = filter_logs(all_logs, level)
        if search:
            all_logs = filter_logs(all_logs, None, search)
        
        all_logs.sort(key=lambda x: x.get('time', ''), reverse=True)
        
        return jsonify({
            "logs": all_logs,
            "total": len(all_logs),
            "files": log_files,
            "params": {
                "lines": max_lines,
                "level": level,
                "search": search
            }
        })
        
    except Exception as e:
        logger.error(f"Erreur endpoint logs/all: {e}")
        return jsonify({
            "error": str(e),
            "logs": [],
            "total": 0
        }), 500

# ==================== ENDPOINT TEST ====================

@app.route("/api/test")
def test():
    """Endpoint de test avec la nouvelle structure (inclut OC)"""
    sftp = None
    transport = None
    
    try:
        logger.info("Test endpoint /api/test")
        sftp, transport = get_sftp_client()
        
        result = {
            "connexion": "OK",
            "general_videos": {"path": DOSSIER_VIDEOS_NORMAL, "exists": False, "count": 0},
            "general_audio": {"path": DOSSIER_AUDIO_NORMAL, "exists": False, "count": 0},
            "general_gps": {"path": DOSSIER_GPS_NORMAL, "exists": False, "count": 0},
            "events_videos": {"path": DOSSIER_VIDEOS_INFRACTION, "exists": False, "count": 0, "subdirs": {}},
            "events_videos_ir": {"path": DOSSIER_VIDEOS_INFRACTION_IR, "exists": False, "count": 0},
            "events_videos_an": {"path": DOSSIER_VIDEOS_INFRACTION_AN, "exists": False, "count": 0},
            "events_audio": {"path": DOSSIER_AUDIO_INFRACTION, "exists": False, "count": 0},
            "events_audio_ir": {"path": DOSSIER_AUDIO_INFRACTION_IR, "exists": False, "count": 0},
            "events_audio_an": {"path": DOSSIER_AUDIO_INFRACTION_AN, "exists": False, "count": 0},
            "events_gps": {"path": DOSSIER_GPS_INFRACTION, "exists": False, "count": 0},
            "events_gps_ir": {"path": DOSSIER_GPS_INFRACTION_IR, "exists": False, "count": 0},
            "events_gps_an": {"path": DOSSIER_GPS_INFRACTION_AN, "exists": False, "count": 0},
            "events_gps_oc": {"path": DOSSIER_GPS_INFRACTION_OC, "exists": False, "count": 0},
            "events_photos": {"path": DOSSIER_PHOTOS_INFRACTION, "exists": False, "count": 0},
            "events_photos_ir": {"path": DOSSIER_PHOTOS_INFRACTION_IR, "exists": False, "count": 0},
            "events_photos_an": {"path": DOSSIER_PHOTOS_INFRACTION_AN, "exists": False, "count": 0},
            "events_photos_oc": {"path": DOSSIER_PHOTOS_INFRACTION_OC, "exists": False, "count": 0},
            "events_pv": {"path": DOSSIER_PV_INFRACTION, "exists": False, "count": 0},
            "events_pv_ir": {"path": DOSSIER_PV_INFRACTION_IR, "exists": False, "count": 0},
            "events_pv_an": {"path": DOSSIER_PV_INFRACTION_AN, "exists": False, "count": 0},
            "events_pv_oc": {"path": DOSSIER_PV_INFRACTION_OC, "exists": False, "count": 0},
        }
        
        # Tester tous les dossiers
        for key, config in result.items():
            if key == "connexion":
                continue
            path = config["path"]
            try:
                files = sftp.listdir(path)
                config["exists"] = True
                config["count"] = len(files)
                if "subdirs" in config:
                    # Compter les sous-dossiers IR, AN et OC pour les dossiers racines
                    for sub in ["IR", "AN", "OC"]:
                        sub_path = f"{path}/{sub}"
                        try:
                            sub_files = sftp.listdir(sub_path)
                            config["subdirs"][sub] = len(sub_files)
                        except:
                            config["subdirs"][sub] = 0
            except Exception as e:
                config["error"] = str(e)
                logger.warning(f"Erreur test {key}: {e}")
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Erreur test: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if sftp:
            try: sftp.close()
            except: pass
        if transport:
            try: transport.close()
            except: pass

# ==================== DECISIONS PV (remplace api2) ====================
from pv_decision import register_pv_decision
register_pv_decision(app, get_sftp_client, DOSSIER_PV_INFRACTION)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
