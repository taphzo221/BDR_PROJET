
import sqlite3, os, io, datetime, hashlib
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, send_file, session, redirect

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT
    PDF_OK = True
except ImportError:
    PDF_OK = False

app = Flask(__name__)
app.secret_key = 'seneau_2026_secret_key'
DB = "seneau.db"

# ═══════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════
def hp(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def cur_user():
    uid = session.get('uid')
    if not uid: return None
    db = get_db()
    u = db.execute("SELECT * FROM utilisateur WHERE id=?", (uid,)).fetchone()
    db.close()
    return dict(u) if u else None

def is_admin():
    """Uniquement admin pur — operations sensibles"""
    u = cur_user()
    return u and u['role'] == 'admin'

def is_staff():
    """Admin + Superviseur — operations courantes de gestion"""
    u = cur_user()
    return u and u['role'] in ('admin', 'superviseur')

def is_agent_only():
    """Agents terrain uniquement"""
    u = cur_user()
    return u and u['role'] in ('agent_releve', 'agent_coupure')

def log_action(action, details=''):
    uid = session.get('uid')
    if not uid: return
    try:
        db = get_db()
        db.execute("INSERT INTO journal(id_utilisateur,action,details) VALUES(?,?,?)", (uid, action, details))
        db.commit(); db.close()
    except: pass

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get('uid'):
            if request.path.startswith('/api/'): return jsonify(error='Non authentifie'), 401
            return redirect('/login')
        return f(*a, **k)
    return w

# ═══════════════════════════════════════════════════════════
#  BASE DE DONNEES
# ═══════════════════════════════════════════════════════════
def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS zone(id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT, quartier TEXT, arrondissement TEXT);
    CREATE TABLE IF NOT EXISTS utilisateur(id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT NOT NULL, prenom TEXT NOT NULL, login TEXT, password_hash TEXT, email TEXT, telephone TEXT, role TEXT DEFAULT 'agent_releve', id_zone INTEGER, statut TEXT DEFAULT 'actif');
    CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY AUTOINCREMENT, id_utilisateur INTEGER, action TEXT, details TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS client(id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT NOT NULL, prenom TEXT NOT NULL, adresse TEXT, telephone TEXT, email TEXT, numero_compte TEXT UNIQUE, type_client TEXT DEFAULT 'particulier', statut_client TEXT DEFAULT 'actif', id_zone INTEGER, date_inscription TEXT DEFAULT (date('now')));
    CREATE TABLE IF NOT EXISTS compteur(id INTEGER PRIMARY KEY AUTOINCREMENT, numero_compteur TEXT UNIQUE NOT NULL, id_client INTEGER, id_zone INTEGER, date_installation TEXT, statut_compteur TEXT DEFAULT 'actif', dernier_index REAL DEFAULT 0, dernier_relevage TEXT);
    CREATE TABLE IF NOT EXISTS tarification(id INTEGER PRIMARY KEY AUTOINCREMENT, date_debut TEXT, tarif_m3 REAL DEFAULT 650, frais_abonnement REAL DEFAULT 2500, frais_coupure_montant REAL DEFAULT 5000, taxe_tva_pct REAL DEFAULT 18, taxe_redevance_pct REAL DEFAULT 3, seuil_impayement_jours INTEGER DEFAULT 30, delai_paiement_jours INTEGER DEFAULT 10, actif INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS tache(id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT NOT NULL, type TEXT NOT NULL, id_zone INTEGER, id_agent INTEGER, date_debut_prevue TEXT, date_fin_prevue TEXT, statut TEXT DEFAULT 'non_demarree', description TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS releve(id INTEGER PRIMARY KEY AUTOINCREMENT, id_compteur INTEGER NOT NULL, date_releve TEXT, index_compteur REAL, volume_m3 REAL, etat_compteur TEXT DEFAULT 'bon', commentaires TEXT, statut_validation TEXT DEFAULT 'en_attente', created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS facture(id INTEGER PRIMARY KEY AUTOINCREMENT, id_client INTEGER NOT NULL, numero_facture TEXT UNIQUE, date_facture TEXT, periode_debut TEXT, periode_fin TEXT, date_limite_paiement TEXT, montant_consommation REAL DEFAULT 0, montant_abonnement REAL DEFAULT 0, montant_taxes REAL DEFAULT 0, montant_frais_coupure REAL DEFAULT 0, montant_total REAL DEFAULT 0, volume_m3 REAL DEFAULT 0, statut_paiement TEXT DEFAULT 'emise');
    CREATE TABLE IF NOT EXISTS paiement(id INTEGER PRIMARY KEY AUTOINCREMENT, id_facture INTEGER, montant_paye REAL, date_paiement TEXT DEFAULT (date('now')), mode_paiement TEXT, reference_paiement TEXT);
    CREATE TABLE IF NOT EXISTS coupure(id INTEGER PRIMARY KEY AUTOINCREMENT, id_compteur INTEGER, id_facture INTEGER, date_coupure_prevue TEXT, statut_coupure TEXT DEFAULT 'en_attente', motif TEXT DEFAULT 'Facture impayee');
    CREATE TABLE IF NOT EXISTS signalement(id INTEGER PRIMARY KEY AUTOINCREMENT, type_compromission TEXT, id_zone INTEGER, contenu TEXT, urgence TEXT DEFAULT 'moyen', traite INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS idee(id INTEGER PRIMARY KEY AUTOINCREMENT, titre TEXT, contenu TEXT, categorie TEXT, statut TEXT DEFAULT 'soumise', id_auteur INTEGER, created_at TEXT DEFAULT (datetime('now','localtime')));
    """)

    # ── MIGRATION : ajouter les colonnes manquantes si elles n'existent pas ──
    cols = [row[1] for row in db.execute("PRAGMA table_info(utilisateur)").fetchall()]
    if 'login' not in cols:
        db.execute("ALTER TABLE utilisateur ADD COLUMN login TEXT")
    if 'password_hash' not in cols:
        db.execute("ALTER TABLE utilisateur ADD COLUMN password_hash TEXT")
    if 'statut' not in cols:
        db.execute("ALTER TABLE utilisateur ADD COLUMN statut TEXT DEFAULT 'actif'")
    if 'role' not in cols:
        db.execute("ALTER TABLE utilisateur ADD COLUMN role TEXT DEFAULT 'agent_releve'")

    # journal
    j_cols = [row[1] for row in db.execute("PRAGMA table_info(journal)").fetchall()]
    if not j_cols:
        db.execute("CREATE TABLE journal(id INTEGER PRIMARY KEY AUTOINCREMENT, id_utilisateur INTEGER, action TEXT, details TEXT, created_at TEXT DEFAULT (datetime('now','localtime')))")

    # idee — colonne id_auteur
    i_cols = [row[1] for row in db.execute("PRAGMA table_info(idee)").fetchall()]
    if 'id_auteur' not in i_cols:
        db.execute("ALTER TABLE idee ADD COLUMN id_auteur INTEGER")

    db.commit()

    # ── SEED si base vide ──
    if db.execute("SELECT COUNT(*) FROM zone").fetchone()[0] == 0:
        seed_data(db)
    else:
        # Base existante : créer les comptes si absents
        _migrate_users(db)

    db.commit(); db.close()


def _migrate_users(db):
    """Ajoute les logins/passwords aux utilisateurs existants si manquants."""
    a, s, g = hp('admin2026'), hp('super2026'), hp('agent2026')

    # Admin
    existing = db.execute("SELECT id FROM utilisateur WHERE login='admin'").fetchone()
    if not existing:
        try:
            db.execute("INSERT INTO utilisateur(nom,prenom,login,password_hash,role) VALUES('Systeme','Admin','admin',?,'admin')", (a,))
        except: pass
    else:
        db.execute("UPDATE utilisateur SET password_hash=? WHERE login='admin'", (a,))

    # Superviseur
    existing = db.execute("SELECT id FROM utilisateur WHERE login='superviseur'").fetchone()
    if not existing:
        try:
            db.execute("INSERT INTO utilisateur(nom,prenom,login,password_hash,role) VALUES('General','Superviseur','superviseur',?,'superviseur')", (s,))
        except: pass

    # Agents existants — assigner login si manquant
    agents = db.execute("SELECT id,nom,prenom FROM utilisateur WHERE login IS NULL OR login=''").fetchall()
    for ag in agents:
        login_val = ag['prenom'].lower()
        db.execute("UPDATE utilisateur SET login=?, password_hash=? WHERE id=?", (login_val, g, ag['id']))

    db.commit()

def seed_data(db):
    a, s, g = hp('admin2026'), hp('super2026'), hp('agent2026')
    db.executescript("""
    INSERT INTO zone(nom,quartier,arrondissement) VALUES('Zone Nord Thies','Mbour Baye','Thies-Nord'),('Zone Sud Thies','Keur Ngom','Thies-Sud'),('Zone Centre Thies','Centre-Ville','Thies-Est');
    INSERT INTO tarification(date_debut,tarif_m3,frais_abonnement,frais_coupure_montant,taxe_tva_pct,taxe_redevance_pct,seuil_impayement_jours,delai_paiement_jours) VALUES('2026-01-01',650,2500,5000,18,3,30,10);
    INSERT INTO client(nom,prenom,adresse,telephone,email,numero_compte,type_client,id_zone) VALUES('Gassama','Sidy','Rue Moussa Diakhaby N2-6','771111111','sgassama@gmail.com','SN-0101-9850','particulier',1),('Diallo','Aminata','Quartier Mbour Baye','772222222','adiallo@gmail.com','SN-0102-3421','particulier',1),('Faye','Oumar','Cite Dakar 3','773333333','ofaye@gmail.com','SN-0203-7812','particulier',2),('Sy','Mariama','Centre-Ville Rue 4','774444444','msy@gmail.com','SN-0301-2209','particulier',3),('Mbaye','Cheikh','Quartier Liberte','775555555','cmbaye@gmail.com','SN-0102-5511','particulier',1),('Diop','Fatou','Residence Soleil','776666666','fdiop@gmail.com','SN-0203-9977','entreprise',2);
    INSERT INTO compteur(numero_compteur,id_client,id_zone,date_installation,statut_compteur,dernier_index,dernier_relevage) VALUES('N0300521',1,1,'2020-01-01','actif',980.5,'2026-03-12'),('N0300644',2,1,'2019-06-15','coupe',450.2,'2026-03-10'),('N0400119',3,2,'2021-03-20','actif',2100.8,'2026-03-11'),('N0500330',4,3,'2020-09-01','suspendu',320.1,'2026-03-09'),('N0301122',5,1,'2022-01-10','coupe',680.3,'2026-03-08'),('N0400888',6,2,'2021-07-05','actif',1540.6,'2026-03-11');
    INSERT INTO releve(id_compteur,date_releve,index_compteur,volume_m3,etat_compteur,statut_validation) VALUES(1,'2026-03-12',980.5,12.3,'bon','valide'),(2,'2026-03-10',450.2,48.0,'bon','en_attente'),(3,'2026-03-11',2100.8,15.8,'bon','valide'),(4,'2026-03-09',320.1,9.2,'bon','en_attente'),(6,'2026-03-11',1540.6,11.5,'bon','valide');
    INSERT INTO tache(nom,type,id_zone,id_agent,date_debut_prevue,date_fin_prevue,statut,description) VALUES('Releve Avril Zone Nord','releve',1,3,'2026-04-01','2026-04-30','en_cours','Relever tous les compteurs zone Nord'),('Releve Avril Zone Sud','releve',2,4,'2026-04-01','2026-04-28','completee','Releve zone Sud'),('Releve Avril Zone Centre','releve',3,5,'2026-04-05','2026-04-30','en_cours','Releve zone Centre'),('Coupures impayes','coupure',1,6,'2026-04-25','2026-04-25','en_cours','Couper les compteurs en defaut'),('Maintenance Zone Nord','maintenance',1,3,'2026-04-28','2026-04-29','non_demarree','Inspection annuelle');
    INSERT INTO facture(id_client,numero_facture,date_facture,periode_debut,periode_fin,date_limite_paiement,montant_consommation,montant_abonnement,montant_taxes,montant_frais_coupure,montant_total,statut_paiement,volume_m3) VALUES(1,'SN-2026-03-001','2026-03-30','2026-02-01','2026-02-28','2026-04-09',7995,2500,1886,0,12381,'payee',12.3),(2,'SN-2026-03-002','2026-03-30','2026-02-01','2026-02-28','2026-03-05',31200,2500,6066,5000,44766,'depassement_delai',48.0),(3,'SN-2026-03-003','2026-03-30','2026-02-01','2026-02-28','2026-04-20',10270,2500,2306,0,15076,'emise',15.8),(5,'SN-2026-03-004','2026-03-30','2026-02-01','2026-02-28','2026-03-05',19500,2500,3960,5000,30960,'depassement_delai',30.0);
    INSERT INTO coupure(id_compteur,id_facture,date_coupure_prevue,statut_coupure) VALUES(2,2,'2026-04-25','effectuee'),(5,4,'2026-04-26','en_attente');
    INSERT INTO signalement(type_compromission,id_zone,contenu,urgence,traite) VALUES('Fuite reseau',1,'Fuite importante rue Keur Mbaye','eleve',0),('Compteur pirate',3,'Compteur N0500221 modifie','critique',0),('Acces refuse',2,'Portail verrouille','moyen',1);
    INSERT INTO idee(titre,contenu,categorie,statut) VALUES('Application mobile agents','App mobile GPS meme hors ligne.','technologie','approuvee'),('Paiement Mobile Money','Orange Money et Wave.','client','en_etude'),('Alertes SMS clients','SMS a la generation de facture.','technologie','soumise');
    """)
    db.execute("INSERT INTO utilisateur(nom,prenom,login,password_hash,role) VALUES('Systeme','Admin','admin',?,'admin')", (a,))
    db.execute("INSERT INTO utilisateur(nom,prenom,login,password_hash,role) VALUES('General','Superviseur','superviseur',?,'superviseur')", (s,))
    db.execute("INSERT INTO utilisateur(nom,prenom,login,password_hash,role,id_zone) VALUES('Diallo','Moussa','moussa',?,'agent_releve',1)", (g,))
    db.execute("INSERT INTO utilisateur(nom,prenom,login,password_hash,role,id_zone) VALUES('Sarr','Ibou','ibou',?,'agent_releve',2)", (g,))
    db.execute("INSERT INTO utilisateur(nom,prenom,login,password_hash,role,id_zone) VALUES('Ndiaye','Awa','awa',?,'agent_releve',3)", (g,))
    db.execute("INSERT INTO utilisateur(nom,prenom,login,password_hash,role,id_zone) VALUES('Ba','Oumar','oumar',?,'agent_coupure',1)", (g,))

# ═══════════════════════════════════════════════════════════
#  PAGE LOGIN
# ═══════════════════════════════════════════════════════════
LOGIN_HTML = """
<!DOCTYPE html><html lang="fr">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEN'EAU — Connexion</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Lato:wght@300;400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;display:flex;font-family:'Lato',sans-serif;background:#102347}
.left{flex:1;background:linear-gradient(160deg,#102347 0%,#1B3A6B 55%,#0d3d1a 100%);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px;position:relative;overflow:hidden}
.left::before{content:'';position:absolute;width:500px;height:500px;border-radius:50%;border:80px solid rgba(141,198,63,.05);top:-100px;right:-100px}
.hero-logo{display:flex;align-items:center;gap:18px;margin-bottom:32px}
.hero-sq{width:68px;height:68px;background:#8DC63F;border-radius:12px;display:flex;align-items:center;justify-content:center}
.hero-brand{font-family:'Playfair Display',serif;font-size:36px;font-weight:700;color:#fff;letter-spacing:2px}
.hero-brand span{color:#8DC63F}
.hero-sub{font-size:11px;color:#F2C94C;letter-spacing:3px;text-transform:uppercase;margin-top:3px}
.hero-title{font-family:'Playfair Display',serif;font-size:26px;color:#fff;max-width:380px;line-height:1.3;text-align:center;margin-bottom:12px}
.hero-title em{color:#8DC63F;font-style:normal}
.hero-desc{font-size:14px;color:rgba(255,255,255,.6);text-align:center;max-width:340px;line-height:1.7;margin-bottom:28px}
.stats{display:flex;background:rgba(0,0,0,.2);border-radius:10px;overflow:hidden}
.stat{padding:14px 24px;text-align:center;border-right:1px solid rgba(255,255,255,.08)}
.stat:last-child{border-right:none}
.stat-n{font-family:'Playfair Display',serif;font-size:22px;font-weight:700;color:#8DC63F}
.stat-l{font-size:10px;color:rgba(255,255,255,.45);text-transform:uppercase;letter-spacing:1.5px;margin-top:3px}
.right{width:460px;background:#fff;display:flex;flex-direction:column;justify-content:center;padding:48px 44px}
.r-logo{display:flex;align-items:center;gap:12px;margin-bottom:28px}
.r-sq{width:44px;height:44px;background:#8DC63F;border-radius:8px;display:flex;align-items:center;justify-content:center}
.r-brand{font-family:'Playfair Display',serif;font-size:20px;font-weight:700;color:#1B3A6B}
.r-brand span{color:#8DC63F}
.r-sub{font-size:9px;color:#7a8f6e;letter-spacing:2px;text-transform:uppercase;margin-top:2px}
h2{font-family:'Playfair Display',serif;font-size:20px;color:#1B3A6B;margin-bottom:5px}
.hsub{font-size:13px;color:#7a8f6e;margin-bottom:24px}
.err{background:#fdecea;border-left:4px solid #c0392b;padding:11px 14px;border-radius:4px;font-size:13px;color:#8a1010;margin-bottom:16px}
.fl{display:flex;flex-direction:column;gap:5px;margin-bottom:14px}
label{font-size:11px;font-weight:500;color:#7a8f6e}
input{padding:12px 14px;border:2px solid #d8e4cc;border-radius:6px;font-size:14px;color:#1B3A6B;font-family:'Lato',sans-serif;outline:none;transition:all .2s;width:100%}
input:focus{border-color:#8DC63F;box-shadow:0 0 0 3px rgba(141,198,63,.12)}
.btn-login{width:100%;padding:13px;background:#8DC63F;color:#102347;border:none;border-radius:6px;font-size:14px;font-weight:700;cursor:pointer;font-family:'Lato',sans-serif;transition:all .2s;margin-top:4px}
.btn-login:hover{background:#6a9e28;color:#fff}
.demo{margin-top:22px;background:#f4f8f2;border-radius:8px;padding:15px;border-left:3px solid #8DC63F}
.dh{font-size:10px;font-weight:700;color:#1B3A6B;text-transform:uppercase;letter-spacing:.8px;margin-bottom:9px}
.dr{display:flex;justify-content:space-between;align-items:center;font-size:12px;padding:5px 0;border-bottom:1px solid #e8f5d0}
.dr:last-child{border-bottom:none}
.dc strong{color:#3a6010}
.rb{display:inline-block;padding:2px 7px;border-radius:10px;font-size:9px;font-weight:700;text-transform:uppercase}
.rba{background:#fff3cd;color:#7a5000}.rbs{background:#e8f5d0;color:#3a6010}.rbg{background:#e6edf7;color:#1B3A6B}
</style></head>
<body>
<div class="left">
  <div class="hero-logo">
    <div class="hero-sq">
      <svg viewBox="0 0 42 42" fill="none" width="46" height="46">
        <path d="M10 32 Q14 18 21 21 Q28 24 32 12" stroke="#1B3A6B" stroke-width="5.5" stroke-linecap="round"/>
        <circle cx="10" cy="32" r="4" fill="#1B3A6B"/><circle cx="32" cy="12" r="4" fill="#1B3A6B"/>
      </svg>
    </div>
    <div><div class="hero-brand">SEN<span>'EAU</span></div><div class="hero-sub">Eau du Senegal</div></div>
  </div>
  <div class="hero-title">L'eau potable pour <em>tous les Senegalais</em></div>
  <div class="hero-desc">Plateforme interne de gestion des releves, factures, coupures et suivi terrain.</div>
  <div class="stats">
    <div class="stat"><div class="stat-n">3,5 M</div><div class="stat-l">Clients</div></div>
    <div class="stat"><div class="stat-n">97%</div><div class="stat-l">Conformite</div></div>
    <div class="stat"><div class="stat-n">24/7</div><div class="stat-l">Service</div></div>
  </div>
</div>
<div class="right">
  <div class="r-logo">
    <div class="r-sq"><svg viewBox="0 0 34 34" fill="none" width="28" height="28"><path d="M8 26 Q12 14 17 17 Q22 20 26 10" stroke="#1B3A6B" stroke-width="5" stroke-linecap="round"/><circle cx="8" cy="26" r="3" fill="#1B3A6B"/><circle cx="26" cy="10" r="3" fill="#1B3A6B"/></svg></div>
    <div><div class="r-brand">SEN<span>'EAU</span></div><div class="r-sub">Plateforme interne</div></div>
  </div>
  <h2>Connexion</h2>
  <div class="hsub">Entrez vos identifiants pour acceder a votre espace</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <div class="fl"><label>Identifiant</label><input type="text" name="login" placeholder="Votre identifiant" required autofocus autocomplete="username"></div>
    <div class="fl"><label>Mot de passe</label><input type="password" name="password" placeholder="Mot de passe" required autocomplete="current-password"></div>
    <button type="submit" class="btn-login">Se connecter</button>
  </form>
  <div class="demo">
    <div class="dh">Comptes de demonstration</div>
    <div class="dr"><span class="dc"><strong>admin</strong> / admin2026</span><span class="rb rba">Administrateur</span></div>
    <div class="dr"><span class="dc"><strong>superviseur</strong> / super2026</span><span class="rb rbs">Superviseur</span></div>
    <div class="dr"><span class="dc"><strong>moussa</strong> / agent2026</span><span class="rb rbg">Agent Zone Nord</span></div>
    <div class="dr"><span class="dc"><strong>ibou</strong> / agent2026</span><span class="rb rbg">Agent Zone Sud</span></div>
    <div class="dr"><span class="dc"><strong>awa</strong> / agent2026</span><span class="rb rbg">Agent Zone Centre</span></div>
    <div class="dr"><span class="dc"><strong>oumar</strong> / agent2026</span><span class="rb rbg">Agent Coupure</span></div>
  </div>
</div>
</body></html>
"""

# ═══════════════════════════════════════════════════════════
#  CSS COMMUN (injecte dans les deux interfaces)
# ═══════════════════════════════════════════════════════════
COMMON_CSS = """
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Lato:wght@300;400;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--gn:#8DC63F;--gd:#6a9e28;--gl:#e8f5d0;--nv:#1B3A6B;--nd:#102347;--gld:#F2C94C;--lt:#f4f8f2;--wh:#fff;--g1:#eef2ea;--g2:#d8e4cc;--g4:#7a8f6e;--rd:#c0392b;--rl:#fdecea;--am:#E89B00;--al:#fff8e6;--rr:8px;--sh:0 2px 14px rgba(27,58,107,.09)}
html,body{height:100%;overflow:hidden;font-family:'Lato',sans-serif;background:var(--lt);color:var(--nv)}
#sh{display:flex;height:100vh}
#sb{background:linear-gradient(175deg,var(--nd) 0%,var(--nv) 100%);display:flex;flex-direction:column;overflow-y:auto;height:100vh}
#sb::-webkit-scrollbar{width:3px}
#sb::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12)}
#mn{flex:1;display:flex;flex-direction:column;height:100vh;overflow:hidden}
#ct{flex:1;overflow-y:auto;padding:26px 28px}
#ct::-webkit-scrollbar{width:6px}
#ct::-webkit-scrollbar-thumb{background:var(--g2);border-radius:3px}
.sbar{height:5px;background:var(--gn);flex-shrink:0}
.slogo{padding:18px 18px 12px}
.suser{padding:11px 18px 13px;border-bottom:1px solid rgba(255,255,255,.08);display:flex;align-items:center;gap:10px}
.ava{width:36px;height:36px;background:var(--gn);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:var(--nv);flex-shrink:0}
.snm{color:#fff;font-size:13px;font-weight:700;line-height:1.2}
.srl{color:rgba(255,255,255,.45);font-size:10px;margin-top:1px}
.rbadge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;margin-top:3px}
.rb-admin{background:rgba(242,201,76,.18);color:var(--gld)}
.rb-sup{background:rgba(141,198,63,.18);color:var(--gn)}
.rb-agent{background:rgba(255,255,255,.1);color:rgba(255,255,255,.6)}
nav{flex:1;padding:8px 0}
.ns{padding:10px 20px 3px;font-size:9px;color:rgba(255,255,255,.28);font-weight:700;letter-spacing:1.5px;text-transform:uppercase}
.ni{display:flex;align-items:center;gap:10px;padding:10px 20px;cursor:pointer;color:rgba(255,255,255,.58);font-size:13px;font-weight:400;transition:all .15s;position:relative;text-decoration:none}
.ni:hover{background:rgba(255,255,255,.06);color:#fff}
.ni.on{background:rgba(141,198,63,.12);color:#fff;font-weight:700}
.ni.on::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--gn)}
.nico{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8;flex-shrink:0}
.nb{margin-left:auto;background:var(--gn);color:var(--nv);font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;line-height:1.4}
.nb.am{background:var(--gld);color:var(--nd)}
.sfoot{padding:12px 18px;border-top:1px solid rgba(255,255,255,.08)}
.logout{display:flex;align-items:center;gap:8px;color:rgba(255,255,255,.5);font-size:12px;text-decoration:none;padding:6px 8px;border-radius:4px;transition:all .15s;border:1px solid rgba(255,255,255,.08)}
.logout:hover{background:rgba(192,57,43,.2);color:#fff}
header{height:60px;background:var(--wh);border-bottom:3px solid var(--gn);display:flex;align-items:center;padding:0 28px;gap:14px;box-shadow:0 2px 8px rgba(27,58,107,.07);flex-shrink:0}
.htitle{flex:1;font-family:'Playfair Display',serif;font-size:16px;font-weight:700;color:var(--nv)}
.hsub{font-size:11px;color:var(--g4);font-family:'Lato',sans-serif;margin-left:6px}
.pg{display:none}.pg.on{display:block}
.ph{margin-bottom:22px;display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.ph h1{font-family:'Playfair Display',serif;font-size:20px;font-weight:700;color:var(--nv);display:flex;align-items:center;gap:10px}
.ph h1::before{content:'';display:block;width:4px;height:24px;background:var(--gn);border-radius:2px}
.ph p{font-size:13px;color:var(--g4);margin-top:4px;padding-left:14px}
.sg{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px}
.sc{background:var(--wh);border-radius:var(--rr);padding:16px 18px;box-shadow:var(--sh);border-top:3px solid var(--gn)}
.sc.am{border-top-color:var(--gld)}.sc.rd{border-top-color:var(--rd)}.sc.bl{border-top-color:#5a9a1a}
.sc-ico{font-size:17px;margin-bottom:9px}
.sc-val{font-family:'Playfair Display',serif;font-size:26px;font-weight:700;color:var(--nv)}
.sc-lbl{font-size:11px;color:var(--g4);margin-top:2px}
.sc-delta{font-size:11px;margin-top:5px}.sc-delta.up{color:var(--gd)}.sc-delta.dn{color:var(--rd)}
.card{background:var(--wh);border-radius:var(--rr);box-shadow:var(--sh);border-top:3px solid var(--gn);margin-bottom:18px}
.card:last-child{margin-bottom:0}
.ch{padding:14px 18px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--g1)}
.ct{font-family:'Playfair Display',serif;font-size:13px;font-weight:700;color:var(--nv)}
.cb{padding:18px}
.g2c{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:16px}
.geq{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{padding:9px 13px;text-align:left;font-size:10px;font-weight:500;color:var(--g4);border-bottom:2px solid var(--gn);background:var(--lt);white-space:nowrap}
tbody tr{border-bottom:1px solid var(--g1);transition:background .12s}
tbody tr:hover{background:var(--gl)}
tbody td{padding:10px 13px;vertical-align:middle}
tbody tr:last-child{border-bottom:none}
.er{text-align:center;padding:32px!important;color:var(--g4)!important}
.badge{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap}
.bg-gn{background:var(--gl);color:var(--gd)}.bg-lm{background:var(--gl);color:#3a6010}
.bg-bl{background:var(--gl);color:var(--gd)}.bg-am{background:var(--al);color:#7a5000}
.bg-rd{background:var(--rl);color:#8a1010}.bg-gr{background:var(--g1);color:var(--nv)}
.dot{width:6px;height:6px;border-radius:50%;display:inline-block}
.dg{background:var(--gn)}.db{background:var(--gn)}.da{background:var(--gld)}.dr{background:var(--rd)}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border-radius:4px;font-size:13px;font-weight:500;cursor:pointer;border:none;transition:all .15s;font-family:'Lato',sans-serif;text-decoration:none;white-space:nowrap}
.bp{background:var(--gn);color:var(--nd)}.bp:hover{background:var(--gd);color:#fff}
.bs{background:var(--wh);color:var(--nv);border:2px solid var(--g2)}.bs:hover{border-color:var(--gn);color:var(--gd)}
.bg{background:var(--gn);color:var(--nv)}.bg:hover{background:var(--gd);color:#fff}
.br2{background:var(--rd);color:#fff}.br2:hover{opacity:.9}
.bgld{background:var(--gld);color:var(--nd)}.bgld:hover{background:#c9a520}
.bsm{padding:5px 11px;font-size:11px}
.fg{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.fl{display:flex;flex-direction:column;gap:4px}.fl.full{grid-column:1/-1}
label{font-size:11px;font-weight:500;color:var(--g4)}
input,select,textarea{padding:9px 12px;border:2px solid var(--g2);border-radius:4px;font-size:13px;font-family:'Lato',sans-serif;color:var(--nv);background:var(--wh);outline:none;width:100%;transition:border-color .2s}
input:focus,select:focus,textarea:focus{border-color:var(--gn);box-shadow:0 0 0 3px rgba(141,198,63,.13)}
textarea{resize:vertical;min-height:80px}
.pb{height:5px;background:var(--g1);border-radius:3px;overflow:hidden;margin-top:4px}
.pf{height:100%;border-radius:3px;transition:width .5s}.pf.g{background:var(--gn)}.pf.b{background:#5a9a1a}
.tl{position:relative;padding-left:22px}
.tl::before{content:'';position:absolute;left:6px;top:4px;bottom:4px;width:2px;background:var(--g2)}
.tli{position:relative;padding-bottom:14px}.tli:last-child{padding-bottom:0}
.tld{position:absolute;left:-22px;width:13px;height:13px;border-radius:50%;top:2px;border:2px solid #fff}
.tld.b{background:var(--gn);box-shadow:0 0 0 2px var(--gl)}.tld.g{background:var(--gn);box-shadow:0 0 0 2px var(--gl)}
.tld.a{background:var(--gld);box-shadow:0 0 0 2px var(--al)}.tld.r{background:var(--rd);box-shadow:0 0 0 2px var(--rl)}
.tli-t{font-size:12px;color:var(--nv)}.tli-d{font-size:11px;color:var(--g4);margin-top:1px}
.alert{padding:11px 15px;border-radius:4px;font-size:13px;display:flex;align-items:flex-start;gap:9px;margin-bottom:12px;line-height:1.55}
.al-g{background:var(--gl);border-left:4px solid var(--gn);color:var(--gd)}
.al-a{background:var(--al);border-left:4px solid var(--gld);color:#6a4500}
.al-r{background:var(--rl);border-left:4px solid var(--rd);color:#7a1a1a}
.ov{display:none;position:fixed;inset:0;background:rgba(16,35,71,.5);z-index:200;align-items:center;justify-content:center;padding:20px}
.ov.op{display:flex}
.modal{background:#fff;border-radius:var(--rr);width:540px;max-width:100%;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(16,35,71,.2);border-top:4px solid var(--gn)}
.modal.lg{width:680px}
.mh{padding:18px 22px 14px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--g1);position:sticky;top:0;background:#fff;z-index:1}
.mt{font-family:'Playfair Display',serif;font-size:15px;font-weight:700;color:var(--nv)}
.mx{width:28px;height:28px;border-radius:4px;border:2px solid var(--g2);background:#fff;cursor:pointer;font-size:17px;color:var(--g4);display:flex;align-items:center;justify-content:center;transition:all .15s}
.mx:hover{border-color:var(--rd);color:var(--rd)}
.mb{padding:18px 22px}.mf{padding:12px 22px;border-top:1px solid var(--g1);display:flex;justify-content:flex-end;gap:10px;position:sticky;bottom:0;background:#fff}
.ic{padding:14px 18px;border:2px solid var(--g1);border-radius:var(--rr);margin-bottom:10px;background:#fff;border-left:4px solid var(--gn);transition:all .15s}
.ic:hover{box-shadow:var(--sh)}
.ic-h{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px}
.ic-t{font-family:'Playfair Display',serif;font-weight:700;font-size:13px;color:var(--nv)}
.ic-m{font-size:10px;color:var(--g4);margin-top:2px}.ic-b{font-size:12px;color:#3d5236;line-height:1.6}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.tag{padding:5px 14px;background:var(--wh);color:var(--nv);border:2px solid var(--g2);border-radius:4px;font-size:11px;font-weight:700;cursor:pointer;transition:all .15s;text-transform:uppercase;letter-spacing:.5px}
.tag.on,.tag:hover{background:var(--gn);color:var(--nv);border-color:var(--gn)}
#toast{position:fixed;bottom:22px;right:22px;z-index:300;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.tm{padding:11px 16px;border-radius:4px;font-size:13px;font-weight:500;box-shadow:0 4px 20px rgba(0,0,0,.15);animation:tin .3s ease;max-width:300px;pointer-events:auto}
.tm.ok{background:var(--gn);color:var(--nv)}.tm.err{background:var(--rd);color:#fff}.tm.inf{background:var(--nv);color:#fff}
@keyframes tin{from{transform:translateX(100%);opacity:0}to{transform:none;opacity:1}}
.mono{font-family:'Courier New',monospace;font-size:11px}
.sb2{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.pager{padding:9px 18px;border-top:1px solid var(--g1);font-size:12px;color:var(--g4)}
.si{padding:5px 11px;background:var(--gl);border-radius:4px;font-size:11px;color:var(--gd);display:inline-flex;align-items:center;gap:6px;margin-top:9px}
#footer{background:var(--nd);padding:10px 28px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;border-top:3px solid var(--gn)}
.ft-nv{display:flex;align-items:center;gap:8px;background:var(--gn);color:var(--nd);padding:8px 18px;border-radius:4px;font-weight:700;font-size:12px;border:none;cursor:pointer;font-family:'Lato',sans-serif;transition:all .15s}
.ft-nv:hover{background:var(--gd);color:#fff}
.ft-cp{font-size:11px;color:rgba(255,255,255,.35)}
.bc{display:flex;align-items:flex-end;gap:8px;height:90px}
.bw{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px}
.bar{width:100%;background:#5a9a1a;border-radius:3px 3px 0 0;cursor:pointer;transition:background .15s}
.bar:hover,.bar.cur{background:var(--gn)}
.bl{font-size:10px;color:var(--g4)}.bv{font-size:10px;color:var(--nv)}
.inv{font-size:12px;line-height:1.5}
.inv-hdr{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px;padding-bottom:14px;border-bottom:3px solid var(--nv)}
.inv-brand{font-family:'Playfair Display',serif;font-size:22px;font-weight:700;color:var(--nv)}
.inv-brand span{color:var(--gn)}.inv-sub{font-size:10px;color:var(--g4);margin-top:1px}
.inv-box{background:var(--gn);color:var(--nd);padding:10px 16px;border-radius:4px;text-align:right}
.inv-box .lbl{font-size:10px;text-transform:uppercase;letter-spacing:1px}
.inv-box .amt{font-size:19px;font-weight:700;font-family:'Playfair Display',serif}
.inv-box .dl{font-size:10px;margin-top:4px}
.inv-sect{background:var(--lt);padding:10px 14px;border-radius:4px;margin-bottom:12px;border-left:3px solid var(--gn)}
.inv-sect h4{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--g4);margin-bottom:7px}
.inv-row{display:flex;justify-content:space-between;gap:12px;margin-bottom:3px;font-size:11px}
.inv-tbl{width:100%;border-collapse:collapse;margin-bottom:12px;font-size:12px}
.inv-tbl th{background:var(--gn);color:var(--nd);padding:7px 10px;text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.4px;font-weight:700}
.inv-tbl td{padding:8px 10px;border-bottom:1px solid var(--g1)}
.inv-tbl tr.tr-tot td{background:var(--gl);font-weight:700;border-top:2px solid var(--gn)}
.inv-total{background:var(--gn);color:var(--nd);padding:14px 18px;border-radius:4px;display:flex;justify-content:space-between;align-items:center}
.inv-total .big{font-size:21px;font-weight:700;font-family:'Playfair Display',serif}
.inv-foot{margin-top:14px;font-size:10px;color:var(--g4);text-align:center;border-top:2px dashed var(--g2);padding-top:10px}
.inv-barcode{font-size:18px;letter-spacing:3px;color:var(--nv);margin-top:5px}
.inv-hist{display:flex;align-items:flex-end;gap:4px;height:50px;margin-top:6px}
.inv-bar-lbl{font-size:8px;color:var(--g4);margin-top:3px;text-align:center;display:block}
@media(max-width:900px){.sg{grid-template-columns:1fr 1fr}.g2c,.geq{grid-template-columns:1fr}.fg{grid-template-columns:1fr}}
</style>
"""

FOOTER_SVG = """
<svg viewBox="0 0 170 44" xmlns="http://www.w3.org/2000/svg" width="148" height="40">
  <rect x="0" y="0" width="44" height="44" rx="7" fill="#8DC63F"/>
  <path d="M10 31 Q14 17 22 20 Q30 23 34 11" stroke="#1B3A6B" stroke-width="5" fill="none" stroke-linecap="round"/>
  <circle cx="10" cy="31" r="3" fill="#1B3A6B"/><circle cx="34" cy="11" r="3" fill="#1B3A6B"/>
  <text x="52" y="22" font-family="Georgia,serif" font-weight="900" font-size="17" fill="#fff" letter-spacing="1">SEN'EAU</text>
  <text x="53" y="36" font-family="Arial,sans-serif" font-size="8" fill="#F2C94C" letter-spacing="2.5" font-weight="700">EAU DU SENEGAL</text>
</svg>
"""

SIDEBAR_LOGO_SVG = """
<svg viewBox="0 0 195 54" xmlns="http://www.w3.org/2000/svg" width="182" height="50">
  <rect x="0" y="1" width="50" height="50" rx="8" fill="#1E4D2B"/>
  <path d="M12 18 Q34 10 34 26 Q34 42 16 42" stroke="#8DC63F" stroke-width="6" fill="none" stroke-linecap="round"/>
  <circle cx="12" cy="18" r="4" fill="#8DC63F"/><circle cx="16" cy="42" r="4" fill="#8DC63F"/>
  <text x="60" y="29" font-family="Georgia,serif" font-weight="900" font-size="20" fill="#fff" letter-spacing="1">SEN'EAU</text>
  <text x="61" y="44" font-family="Arial,sans-serif" font-size="9" fill="#8DC63F" letter-spacing="2.5" font-weight="700">EAU DU SENEGAL</text>
</svg>
"""

# ═══════════════════════════════════════════════════════════
#  INTERFACE AGENT
# ═══════════════════════════════════════════════════════════
AGENT_HTML = """
<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEN'EAU — Espace Agent</title>""" + COMMON_CSS + """
<style>#sb{width:235px;min-width:235px}#mn{}</style>
</head><body>
<div id="sh">
<div id="sb">
  <div class="sbar"></div>
  <div class="slogo">""" + SIDEBAR_LOGO_SVG + """</div>
  <div class="suser">
    <div class="ava" id="ag-av">AG</div>
    <div>
      <div class="snm" id="ag-nm">Agent</div>
      <div class="srl" id="ag-rl">—</div>
      <div class="rbadge rb-agent" id="ag-rb">Agent</div>
      <div style="background:rgba(141,198,63,.18);color:#8DC63F;font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;margin-top:3px;display:inline-block" id="ag-zone">—</div>
    </div>
  </div>
  <nav>
    <div class="ns">Mon espace</div>
    <a class="ni on" href="#" onclick="nav('accueil',this)"><svg class="nico" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>Tableau de bord</a>
    <a class="ni" href="#" onclick="nav('mes-taches',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>Mes taches</a>
    <a class="ni" href="#" onclick="nav('mes-releves',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>Saisir un releve</a>
    <div class="ns">Consultation</div>
    <a class="ni" href="#" onclick="nav('clients-zone',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>Clients de ma zone</a>
    <a class="ni" href="#" onclick="nav('factures-zone',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>Factures (lecture)</a>
    <div class="ns">Participation</div>
    <a class="ni" href="#" onclick="nav('idees-agent',this)"><svg class="nico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>Boite a idees</a>
    <a class="ni" href="#" onclick="nav('signaler',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>Signaler un incident</a>
  </nav>
  <div class="sfoot"><a href="/logout" class="logout"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>Deconnexion</a></div>
</div>
<div id="mn">
  <header><div class="htitle" id="h-title">Tableau de bord<span class="hsub" id="h-sub"></span></div></header>
  <div id="ct">

    <div class="pg on" id="p-accueil">
      <div class="ph"><div><h1>Bienvenue</h1><p id="ag-welcome">Votre espace de travail</p></div></div>
      <div class="sg" id="ag-stats"></div>
      <div class="geq">
        <div class="card"><div class="ch"><span class="ct">Mes taches actives</span></div><div class="cb tw" style="padding:0"><table><thead><tr><th>Tache</th><th>Type</th><th>Statut</th></tr></thead><tbody id="ag-t-mini"></tbody></table></div></div>
        <div class="card"><div class="ch"><span class="ct">Mes derniers releves</span></div><div class="cb tw" style="padding:0"><table><thead><tr><th>Compteur</th><th>Date</th><th>Volume</th><th>Validation</th></tr></thead><tbody id="ag-r-mini"></tbody></table></div></div>
      </div>
      <div id="ag-alerts"></div>
    </div>

    <div class="pg" id="p-mes-taches">
      <div class="ph"><div><h1>Mes taches</h1><p>Taches qui me sont assignees</p></div></div>
      <div class="card"><div class="cb tw" style="padding:0"><table><thead><tr><th>Tache</th><th>Type</th><th>Zone</th><th>Periode</th><th>Statut</th><th>Action</th></tr></thead><tbody id="ag-tl"></tbody></table></div></div>
    </div>

    <div class="pg" id="p-mes-releves">
      <div class="ph"><div><h1>Saisir un releve</h1><p>Compteurs de votre zone uniquement</p></div></div>
      <div class="geq">
        <div class="card">
          <div class="ch"><span class="ct">Nouveau releve</span></div>
          <div class="cb">
            <div class="fg">
              <div class="fl full"><label>Compteur *</label><select id="ag-cpt"></select></div>
              <div class="fl"><label>Date *</label><input type="date" id="ag-date"></div>
              <div class="fl"><label>Index lu (m3) *</label><input type="number" id="ag-idx" step="0.001" placeholder="ex: 1245.300"></div>
              <div class="fl"><label>Etat compteur</label><select id="ag-etat"><option value="bon">Bon etat</option><option value="defectueux">Defectueux</option><option value="bloque">Bloque</option><option value="inaccessible">Inaccessible</option></select></div>
              <div class="fl full"><label>Commentaires</label><textarea id="ag-comm" placeholder="Observations..."></textarea></div>
            </div>
            <div id="ag-cpt-info" style="margin:10px 0"></div>
            <button class="btn bp" onclick="saveRel()">Enregistrer le releve</button>
          </div>
        </div>
        <div class="card">
          <div class="ch"><span class="ct">Mes releves recents</span><button class="btn bs bsm" onclick="loadRel()">Actualiser</button></div>
          <div class="cb tw" style="padding:0"><table><thead><tr><th>Compteur</th><th>Date</th><th>Volume</th><th>Validation</th></tr></thead><tbody id="ag-rl"></tbody></table></div>
        </div>
      </div>
    </div>

    <div class="pg" id="p-clients-zone">
      <div class="ph"><div><h1>Clients de ma zone</h1><p>Consultation uniquement</p></div></div>
      <div class="card">
        <div class="ch"><span class="ct">Liste des clients</span><input type="text" id="ag-cl-s" placeholder="Rechercher..." oninput="loadCLAgent()" style="width:180px"></div>
        <div class="cb tw" style="padding:0"><table><thead><tr><th>N Compte</th><th>Client</th><th>Compteur</th><th>Dernier releve</th><th>Statut</th></tr></thead><tbody id="ag-cl"></tbody></table></div>
        <div class="pager" id="ag-cl-n"></div>
      </div>
    </div>

    <div class="pg" id="p-factures-zone">
      <div class="ph"><div><h1>Factures — Lecture seule</h1><p>Factures de votre zone</p></div></div>
      <div class="alert al-g"><span>i</span><div>Vous pouvez consulter les factures. La gestion est reservee aux superviseurs et administrateurs.</div></div>
      <div class="card"><div class="cb tw" style="padding:0"><table><thead><tr><th>N Facture</th><th>Client</th><th>Periode</th><th>Volume</th><th>Montant TTC</th><th>Statut</th></tr></thead><tbody id="ag-fl"></tbody></table></div></div>
    </div>

    <div class="pg" id="p-idees-agent">
      <div class="ph"><div><h1>Boite a idees</h1><p>Soumettez et consultez les suggestions</p></div><button class="btn bp" onclick="om('m-idee-ag')">+ Soumettre une idee</button></div>
      <div id="ag-idees"></div>
    </div>

    <div class="pg" id="p-signaler">
      <div class="ph"><div><h1>Signaler un incident</h1><p>Tout probleme sur le reseau</p></div></div>
      <div class="geq">
        <div class="card">
          <div class="ch"><span class="ct">Nouveau signalement</span></div>
          <div class="cb">
            <div class="fg">
              <div class="fl"><label>Type *</label><select id="ag-st"><option>Fuite reseau</option><option>Compteur pirate</option><option>Acces refuse</option><option>Coupure illegale</option><option>Pression insuffisante</option><option>Autre</option></select></div>
              <div class="fl"><label>Urgence</label><select id="ag-su"><option value="faible">Faible</option><option value="moyen">Moyen</option><option value="eleve">Eleve</option><option value="critique">Critique</option></select></div>
              <div class="fl full"><label>Description *</label><textarea id="ag-sd" placeholder="Decrivez l'incident..."></textarea></div>
            </div>
            <div style="margin-top:12px"><button class="btn br2" onclick="saveSign()">Signaler l'incident</button></div>
          </div>
        </div>
        <div class="card"><div class="ch"><span class="ct">Mes signalements</span></div><div class="cb tw" style="padding:0"><table><thead><tr><th>Date</th><th>Type</th><th>Urgence</th><th>Statut</th></tr></thead><tbody id="ag-sl"></tbody></table></div></div>
      </div>
    </div>

  </div>
  <div id="footer">""" + FOOTER_SVG + """<div class="ft-cp">Espace agent &mdash; &copy; 2026 Sen'Eau</div><button class="ft-nv" onclick="location.href='tel:800001111'">📞 N° Vert &nbsp; 800 00 11 11</button></div>
</div>
</div>
<div class="ov" id="m-idee-ag"><div class="modal">
  <div class="mh"><span class="mt">Soumettre une idee</span><button class="mx" onclick="cm('m-idee-ag')">x</button></div>
  <div class="mb"><div class="fg">
    <div class="fl full"><label>Titre *</label><input id="ai-t" placeholder="Un titre clair"></div>
    <div class="fl full"><label>Description *</label><textarea id="ai-c" placeholder="Decrivez votre idee..." style="min-height:90px"></textarea></div>
    <div class="fl"><label>Categorie</label><select id="ai-k"><option value="technologie">Technologie</option><option value="process">Process</option><option value="client">Relation client</option><option value="rh">RH</option></select></div>
  </div></div>
  <div class="mf"><button class="btn bs" onclick="cm('m-idee-ag')">Annuler</button><button class="btn bp" onclick="saveIdee()">Soumettre</button></div>
</div></div>
<div id="toast"></div>
<script>
const api=async(url,o={})=>{const r=await fetch(url,{headers:{'Content-Type':'application/json'},...o});if(r.status===401){location='/login';return {error:'401'};}return r.json();};
const get=u=>api(u);
const post=(u,d)=>api(u,{method:'POST',body:JSON.stringify(d)});
const patch=(u,d)=>api(u,{method:'PATCH',body:JSON.stringify(d)});
const fmt=n=>new Intl.NumberFormat('fr-FR').format(Math.round(n||0))+' FCFA';
const fmtm=n=>(parseFloat(n||0)).toFixed(3)+' m3';
function toast(m,t='ok'){const d=document.createElement('div');d.className='tm '+t;d.textContent=m;document.getElementById('toast').appendChild(d);setTimeout(()=>d.remove(),3500);}
function om(id){document.getElementById(id).classList.add('op');}
function cm(id){document.getElementById(id).classList.remove('op');}
document.querySelectorAll('.ov').forEach(o=>o.addEventListener('click',e=>{if(e.target===o)o.classList.remove('op');}));
const TITLES={accueil:'Tableau de bord','mes-taches':'Mes taches','mes-releves':'Saisir un releve','clients-zone':'Clients de ma zone','factures-zone':'Factures — Lecture','idees-agent':'Boite a idees',signaler:'Signaler un incident'};
const LOADS={accueil:loadAccueil,'mes-taches':loadTachesAg,'mes-releves':()=>{loadRel();document.getElementById('ag-date').value=new Date().toISOString().split('T')[0];},'clients-zone':loadCLAgent,'factures-zone':loadFactAg,'idees-agent':loadIdeesAg,signaler:loadSignAg};
function nav(p,el){
  document.querySelectorAll('.pg').forEach(x=>x.classList.remove('on'));
  document.getElementById('p-'+p).classList.add('on');
  document.querySelectorAll('.ni').forEach(n=>n.classList.remove('on'));
  if(el)el.classList.add('on');
  document.getElementById('h-title').innerHTML=TITLES[p]+'<span class="hsub" id="h-sub"></span>';
  setHSub();if(LOADS[p])LOADS[p]();return false;
}
function setHSub(){const s=document.getElementById('h-sub');if(s)s.textContent=' — '+new Date().toLocaleDateString('fr-FR',{day:'numeric',month:'long',year:'numeric'});}
function sBadge(s){const m={actif:'<span class="badge bg-gn"><span class="dot dg"></span>Actif</span>',coupe:'<span class="badge bg-rd"><span class="dot dr"></span>Coupe</span>',suspendu:'<span class="badge bg-am"><span class="dot da"></span>Suspendu</span>'};return m[s]||`<span class="badge bg-gr">${s}</span>`;}
function fBadge(s){const m={emise:'<span class="badge bg-bl">Emise</span>',payee:'<span class="badge bg-gn"><span class="dot dg"></span>Payee</span>',depassement_delai:'<span class="badge bg-am"><span class="dot da"></span>En retard</span>'};return m[s]||`<span class="badge bg-gr">${s}</span>`;}
function vBadge(v){if(v==='valide')return'<span class="badge bg-gn">Valide</span>';if(v==='rejete')return'<span class="badge bg-rd">Rejete</span>';return'<span class="badge bg-am">En attente</span>';}
function tBadge(t){const m={releve:'bg-bl Releve',coupure:'bg-rd Coupure',maintenance:'bg-am Maintenance',reconnexion:'bg-lm Reconnexion'};const[c,l]=(m[t]||'bg-gr '+t).split(' ');return`<span class="badge ${c}">${l}</span>`;}

async function initAgent(){
  const me=await get('/api/me');
  document.getElementById('ag-av').textContent=(me.prenom[0]+me.nom[0]).toUpperCase();
  document.getElementById('ag-nm').textContent=me.prenom+' '+me.nom;
  const rl={agent_releve:'Agent Releve',agent_coupure:'Agent Coupure',superviseur:'Superviseur'};
  document.getElementById('ag-rl').textContent=rl[me.role]||me.role;
  document.getElementById('ag-zone').textContent=me.zone||'Toutes zones';
  document.getElementById('ag-welcome').textContent='Bonjour '+me.prenom+' — '+new Date().toLocaleDateString('fr-FR',{weekday:'long',day:'numeric',month:'long'});
  const cpts=await get('/api/compteurs');
  document.getElementById('ag-cpt').innerHTML='<option value="">Selectionner un compteur...</option>'+cpts.map(c=>`<option value="${c.id}">${c.numero_compteur} — ${c.client}</option>`).join('');
  await loadAccueil();setHSub();
}

async function loadAccueil(){
  const d=await get('/api/agent/dashboard');
  document.getElementById('ag-stats').innerHTML=`
    <div class="sc"><div class="sc-ico">📋</div><div class="sc-val">${d.mes_taches}</div><div class="sc-lbl">Taches actives</div></div>
    <div class="sc"><div class="sc-ico">📊</div><div class="sc-val">${d.mes_releves}</div><div class="sc-lbl">Releves ce mois</div></div>
    <div class="sc am"><div class="sc-ico">⚠</div><div class="sc-val">${d.releves_attente}</div><div class="sc-lbl">En attente validation</div></div>`;
  document.getElementById('ag-t-mini').innerHTML=(d.taches||[]).map(t=>`<tr><td><strong>${t.nom}</strong></td><td>${tBadge(t.type)}</td><td><span class="badge bg-${t.statut==='en_cours'?'bl':t.statut==='completee'?'gn':'gr'}">${t.statut.replace('_',' ')}</span></td></tr>`).join('')||'<tr><td colspan="3" class="er">Aucune tache assignee</td></tr>';
  document.getElementById('ag-r-mini').innerHTML=(d.derniers_releves||[]).map(r=>`<tr><td><span class="mono">${r.compteur}</span></td><td style="font-size:11px">${r.date_releve}</td><td>${fmtm(r.volume_m3)}</td><td>${vBadge(r.statut_validation)}</td></tr>`).join('')||'<tr><td colspan="4" class="er">Aucun releve</td></tr>';
  document.getElementById('ag-alerts').innerHTML=d.releves_attente>0?`<div class="alert al-a"><span>!</span><div><strong>${d.releves_attente} releve(s)</strong> en attente de validation par un superviseur.</div></div>`:'';
}

async function loadTachesAg(){
  const d=await get('/api/agent/taches');
  document.getElementById('ag-tl').innerHTML=d.length?d.map(t=>`<tr>
    <td><strong>${t.nom}</strong>${t.description?`<div style="font-size:11px;color:var(--g4)">${t.description}</div>`:''}</td>
    <td>${tBadge(t.type)}</td>
    <td>${t.zone||'—'}</td>
    <td style="font-size:11px">${t.date_debut_prevue||'—'} → ${t.date_fin_prevue||'—'}</td>
    <td><span class="badge bg-${t.statut==='en_cours'?'bl':t.statut==='completee'?'gn':'gr'}">${t.statut.replace('_',' ')}</span></td>
    <td>${t.statut==='non_demarree'?`<button class="btn bg bsm" onclick="majT(${t.id},'en_cours')">Demarrer</button>`:t.statut==='en_cours'?`<button class="btn bp bsm" onclick="majT(${t.id},'completee')">Terminer</button>`:'—'}</td>
  </tr>`).join(''):'<tr><td colspan="6" class="er">Aucune tache assignee</td></tr>';
}
async function majT(id,s){await patch(`/api/taches/${id}`,{statut:s});toast('Statut mis a jour','ok');loadTachesAg();loadAccueil();}

async function loadRel(){
  const d=await get('/api/agent/releves');
  document.getElementById('ag-rl').innerHTML=d.length?d.map(r=>`<tr>
    <td><span class="mono">${r.compteur}</span></td>
    <td style="font-size:11px">${r.date_releve}</td>
    <td style="font-weight:600;color:${r.volume_m3>30?'var(--am)':'var(--nv)'}">${fmtm(r.volume_m3)}${r.volume_m3>30?' ⚠':''}</td>
    <td>${vBadge(r.statut_validation)}</td>
  </tr>`).join(''):'<tr><td colspan="4" class="er">Aucun releve</td></tr>';
}

document.getElementById('ag-cpt').addEventListener('change',async function(){
  if(!this.value){document.getElementById('ag-cpt-info').innerHTML='';return;}
  const c=await get('/api/compteur/'+this.value);
  if(c&&!c.error)document.getElementById('ag-cpt-info').innerHTML=`<div class="alert al-g" style="margin:0;font-size:12px">Client : <strong>${c.client}</strong> — Dernier index : <strong>${fmtm(c.dernier_index)}</strong></div>`;
});

async function saveRel(){
  const id=document.getElementById('ag-cpt').value,date=document.getElementById('ag-date').value,idx=document.getElementById('ag-idx').value;
  if(!id||!date||!idx){toast('Remplissez tous les champs *','err');return;}
  const r=await post('/api/releves',{id_compteur:id,date_releve:date,index_compteur:parseFloat(idx),etat_compteur:document.getElementById('ag-etat').value,commentaires:document.getElementById('ag-comm').value});
  if(r.error){toast(r.error,'err');return;}
  toast('Releve enregistre — Volume : '+fmtm(r.volume),'ok');
  document.getElementById('ag-idx').value='';document.getElementById('ag-comm').value='';document.getElementById('ag-cpt-info').innerHTML='';
  loadRel();loadAccueil();
}

async function loadCLAgent(){
  const s=document.getElementById('ag-cl-s').value;
  const d=await get('/api/agent/clients?s='+encodeURIComponent(s));
  document.getElementById('ag-cl-n').textContent=d.length+' client(s)';
  document.getElementById('ag-cl').innerHTML=d.length?d.map(r=>`<tr>
    <td><span class="mono">${r.numero_compte||'—'}</span></td>
    <td><strong>${r.nom} ${r.prenom}</strong><div style="font-size:11px;color:var(--g4)">${r.adresse||''}</div></td>
    <td><span class="mono">${r.compteur||'—'}</span></td>
    <td style="font-size:11px">${r.dernier_relevage||'—'}</td>
    <td>${sBadge(r.statut_compteur)}</td>
  </tr>`).join(''):'<tr><td colspan="5" class="er">Aucun client</td></tr>';
}

async function loadFactAg(){
  const d=await get('/api/agent/factures');
  document.getElementById('ag-fl').innerHTML=d.length?d.map(f=>`<tr>
    <td><span class="mono">${f.numero_facture}</span></td>
    <td><strong>${f.client}</strong></td>
    <td style="font-size:11px">${f.periode_debut} → ${f.periode_fin}</td>
    <td style="font-size:11px">${fmtm(f.volume_m3)}</td>
    <td style="font-weight:600">${fmt(f.montant_total)}</td>
    <td>${fBadge(f.statut_paiement)}</td>
  </tr>`).join(''):'<tr><td colspan="6" class="er">Aucune facture</td></tr>';
}

async function loadIdeesAg(){
  const d=await get('/api/idees');
  const bm={soumise:'bg-bl Soumise',en_etude:'bg-am En etude',approuvee:'bg-gn Approuvee',rejetee:'bg-rd Rejetee'};
  document.getElementById('ag-idees').innerHTML=d.length?d.map(i=>{
    const[c,...l]=(bm[i.statut]||'bg-gr '+i.statut).split(' ');
    return`<div class="ic"><div class="ic-h"><div><div class="ic-t">${i.titre}</div><div class="ic-m">${i.categorie||''} — ${i.created_at?.substring(0,10)||''}</div></div><span class="badge ${c}">${l.join(' ')}</span></div><div class="ic-b">${i.contenu}</div></div>`;
  }).join(''):'<div class="alert al-g">Aucune idee. Soyez le premier a soumettre !</div>';
}

async function saveIdee(){
  const t=document.getElementById('ai-t').value.trim(),c=document.getElementById('ai-c').value.trim();
  if(!t||!c){toast('Titre et description requis','err');return;}
  await post('/api/idees',{titre:t,contenu:c,categorie:document.getElementById('ai-k').value});
  cm('m-idee-ag');toast('Idee soumise !','ok');
  ['ai-t','ai-c'].forEach(id=>document.getElementById(id).value='');
  loadIdeesAg();
}

async function loadSignAg(){
  const d=await get('/api/agent/signalements');
  document.getElementById('ag-sl').innerHTML=d.length?d.map(s=>`<tr>
    <td style="font-size:11px">${s.created_at?.substring(0,10)||'—'}</td>
    <td><span class="badge bg-am">${s.type_compromission}</span></td>
    <td><span class="badge bg-${s.urgence==='critique'||s.urgence==='eleve'?'rd':'am'}">${s.urgence}</span></td>
    <td>${s.traite?'<span class="badge bg-gn">Traite</span>':'<span class="badge bg-am">En cours</span>'}</td>
  </tr>`).join(''):'<tr><td colspan="4" class="er">Aucun signalement</td></tr>';
}

async function saveSign(){
  const c=document.getElementById('ag-sd').value.trim();
  if(!c){toast('Description requise','err');return;}
  await post('/api/signalements',{type_compromission:document.getElementById('ag-st').value,contenu:c,urgence:document.getElementById('ag-su').value});
  toast('Signalement enregistre','ok');document.getElementById('ag-sd').value='';loadSignAg();
}

window.addEventListener('DOMContentLoaded',()=>{setHSub();initAgent();});
</script>
</body></html>
"""

# ═══════════════════════════════════════════════════════════
#  INTERFACE ADMIN/SUPERVISEUR
# ═══════════════════════════════════════════════════════════
ADMIN_HTML = """
<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEN'EAU — Administration</title>""" + COMMON_CSS + """
<style>#sb{width:252px;min-width:252px}.j-admin td{background:#fffde7!important}.j-sup td{background:#f1f8e9!important}</style>
</head><body>
<div id="sh">
<div id="sb">
  <div class="sbar"></div>
  <div class="slogo">""" + SIDEBAR_LOGO_SVG + """</div>
  <div class="suser">
    <div class="ava" id="ad-av">AD</div>
    <div>
      <div class="snm" id="ad-nm">Administrateur</div>
      <div class="srl" id="ad-rl">—</div>
      <div class="rbadge" id="ad-rb">Admin</div>
    </div>
  </div>
  <nav>
    <div class="ns">Principal</div>
    <a class="ni on" href="#" onclick="nav('dashboard',this)"><svg class="nico" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>Tableau de bord</a>
    <div class="ns">Terrain</div>
    <a class="ni" href="#" onclick="nav('clients',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>Clients &amp; Compteurs</a>
    <a class="ni" href="#" onclick="nav('taches',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>Taches de terrain<span class="nb" id="nb-t">0</span></a>
    <a class="ni" href="#" onclick="nav('releves',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>Releves compteurs</a>
    <div class="ns">Finance</div>
    <a class="ni" href="#" onclick="nav('factures',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>Facturation<span class="nb am" id="nb-f">0</span></a>
    <a class="ni" href="#" onclick="nav('coupures',this)"><svg class="nico" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>Coupures &amp; Reconnexion</a>
    <div class="ns">Participation</div>
    <a class="ni" href="#" onclick="nav('signalements',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>Signalements</a>
    <a class="ni" href="#" onclick="nav('idees',this)"><svg class="nico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>Boite a idees</a>
    <div class="ns">Administration</div>
    <a class="ni" href="#" onclick="nav('journal',this)"><svg class="nico" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>Journal d'activite</a>
    <a class="ni" id="ni-tarif" href="#" onclick="nav('tarifs',this)"><svg class="nico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>Tarification</a>
  </nav>
  <div class="sfoot">
    <div style="font-size:11px;padding:4px 10px;border-radius:20px;display:inline-flex;align-items:center;gap:5px;background:rgba(141,198,63,.18);color:#8DC63F;font-weight:700">SQLite connecte</div>
    <a href="/logout" class="logout" style="margin-top:8px"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>Deconnexion</a>
  </div>
</div>
<div id="mn">
  <header><div class="htitle" id="h-title">Tableau de bord<span class="hsub" id="h-sub"></span></div><button class="btn bgld bsm" onclick="openNotif()">Notifications</button></header>
  <div id="ct">

    <div class="pg on" id="p-dashboard">
      <div class="ph"><div><h1>Vue d'ensemble</h1><p id="dash-sub">Chargement...</p></div></div>
      <div class="sg" id="dash-stats"></div>
      <div class="g2c">
        <div class="card"><div class="ch"><span class="ct">Consommation mensuelle (m3)</span></div>
          <div class="cb"><div class="bc" id="barchart"></div><div style="margin-top:14px;display:flex;flex-direction:column;gap:10px" id="dash-prog"></div></div>
        </div>
        <div class="card"><div class="ch"><span class="ct">Activite recente</span></div><div class="cb"><div class="tl" id="dash-tl"></div></div></div>
      </div>
      <div id="dash-alerts"></div>
      <div class="card"><div class="ch"><span class="ct">Taches en cours</span><a class="btn bs bsm" href="#" onclick="nav('taches',null)">Voir tout</a></div><div class="cb tw" style="padding:0"><table><thead><tr><th>Tache</th><th>Type</th><th>Zone</th><th>Agent</th><th>Statut</th></tr></thead><tbody id="dash-taches"></tbody></table></div></div>
    </div>

    <div class="pg" id="p-clients">
      <div class="ph"><div><h1>Clients &amp; Compteurs</h1><p>Gestion complete des abonnes</p></div><button class="btn bp" onclick="om('m-client')">+ Nouveau client</button></div>
      <div class="sg" id="cl-stats"></div>
      <div class="card">
        <div class="ch"><span class="ct">Liste des clients</span>
          <div class="sb2"><input type="text" id="cl-s" placeholder="Nom, compte..." oninput="loadClients()" style="width:190px"><select id="cl-z" onchange="loadClients()" style="width:140px"><option value="">Toutes zones</option></select><select id="cl-st" onchange="loadClients()" style="width:130px"><option value="">Tous statuts</option><option value="actif">Actif</option><option value="coupe">Coupe</option><option value="suspendu">Suspendu</option></select></div>
        </div>
        <div class="cb tw" style="padding:0"><table><thead><tr><th>N Compte</th><th>Client</th><th>Zone</th><th>Compteur</th><th>Dernier releve</th><th>Statut</th><th>Actions</th></tr></thead><tbody id="cl-tbody"></tbody></table></div>
        <div class="pager" id="cl-n"></div>
      </div>
    </div>

    <div class="pg" id="p-taches">
      <div class="ph"><div><h1>Taches de terrain</h1><p>Gestion des taches agents</p></div><button class="btn bp" onclick="om('m-tache')">+ Creer une tache</button></div>
      <div class="tags"><div class="tag on" onclick="filterT('',this)">Toutes</div><div class="tag" onclick="filterT('releve',this)">Releves</div><div class="tag" onclick="filterT('coupure',this)">Coupures</div><div class="tag" onclick="filterT('maintenance',this)">Maintenance</div></div>
      <div class="card"><div class="cb tw" style="padding:0"><table><thead><tr><th>Tache</th><th>Type</th><th>Zone</th><th>Agent</th><th>Periode</th><th>Statut</th><th>Actions</th></tr></thead><tbody id="ta-tbody"></tbody></table></div></div>
    </div>

    <div class="pg" id="p-releves">
      <div class="ph"><div><h1>Releves — Validation</h1><p>Valider ou rejeter les releves agents</p></div></div>
      <div class="geq">
        <div class="card">
          <div class="ch"><span class="ct">Saisir un releve</span></div>
          <div class="cb">
            <div class="fg">
              <div class="fl full"><label>Compteur *</label><select id="r-cpt"></select></div>
              <div class="fl"><label>Date *</label><input type="date" id="r-date"></div>
              <div class="fl"><label>Index lu (m3) *</label><input type="number" id="r-idx" step="0.001" placeholder="ex: 1245.300"></div>
              <div class="fl"><label>Etat</label><select id="r-etat"><option value="bon">Bon</option><option value="defectueux">Defectueux</option><option value="inaccessible">Inaccessible</option></select></div>
              <div class="fl full"><label>Commentaires</label><textarea id="r-comm" placeholder="Observations..."></textarea></div>
            </div>
            <div id="r-info" style="margin:10px 0"></div>
            <button class="btn bp" onclick="saveReleve()">Enregistrer</button>
          </div>
        </div>
        <div class="card"><div class="ch"><span class="ct">Releves recents</span><button class="btn bs bsm" onclick="loadReleves()">Actualiser</button></div><div class="cb tw" style="padding:0"><table><thead><tr><th>Compteur / Client</th><th>Date</th><th>Volume</th><th>Validation</th></tr></thead><tbody id="rel-tbody"></tbody></table></div></div>
      </div>
    </div>

    <div class="pg" id="p-factures">
      <div class="ph"><div><h1>Facturation</h1><p>Generation et gestion complete</p></div><button class="btn bp" onclick="om('m-gfact')">+ Generer une facture</button></div>
      <div class="sg" id="fact-stats"></div>
      <div class="card">
        <div class="ch"><span class="ct">Liste des factures</span>
          <div class="sb2"><select id="f-st" onchange="loadFactures()" style="width:150px"><option value="">Tous statuts</option><option value="emise">Emise</option><option value="payee">Payee</option><option value="depassement_delai">En retard</option></select><input type="month" id="f-mo" onchange="loadFactures()" style="width:155px"></div>
        </div>
        <div class="cb tw" style="padding:0"><table><thead><tr><th>N Facture</th><th>Client</th><th>Periode</th><th>Volume</th><th>Montant TTC</th><th>Date limite</th><th>Statut</th><th>Actions</th></tr></thead><tbody id="fact-tbody"></tbody></table></div>
      </div>
    </div>

    <div class="pg" id="p-coupures">
      <div class="ph"><div><h1>Coupures &amp; Reconnexions</h1><p>Recouvrement force</p></div><button class="btn br2" onclick="genCoupures()">Generer coupures auto</button></div>
      <div class="alert al-a"><span>!</span><div>Toute facture impayee apres <strong>30 jours</strong> declenche un bon de coupure de <strong>5 000 FCFA</strong>.</div></div>
      <div class="card"><div class="ch"><span class="ct">Coupures actives</span></div><div class="cb tw" style="padding:0"><table><thead><tr><th>Client</th><th>Compteur</th><th>Montant</th><th>Frais</th><th>Total du</th><th>Statut</th><th>Actions</th></tr></thead><tbody id="coup-tbody"></tbody></table></div></div>
    </div>

    <div class="pg" id="p-signalements">
      <div class="ph"><div><h1>Signalements</h1><p>Incidents reseau</p></div></div>
      <div class="card"><div class="cb tw" style="padding:0"><table><thead><tr><th>Date</th><th>Type</th><th>Zone</th><th>Description</th><th>Urgence</th><th>Statut</th><th>Action</th></tr></thead><tbody id="sig-tbody"></tbody></table></div></div>
    </div>

    <div class="pg" id="p-idees">
      <div class="ph"><div><h1>Boite a idees</h1><p>Approbation des suggestions</p></div></div>
      <div id="idees-list"></div>
    </div>

    <div class="pg" id="p-journal">
      <div class="ph"><div><h1>Journal d'activite</h1><p>Toutes les actions enregistrees</p></div></div>
      <div class="card">
        <div class="ch"><span class="ct">Historique</span><input type="text" id="j-s" placeholder="Filtrer..." oninput="loadJournal()" style="width:210px"></div>
        <div class="cb tw" style="padding:0"><table><thead><tr><th>Date / Heure</th><th>Utilisateur</th><th>Role</th><th>Action</th><th>Details</th></tr></thead><tbody id="j-tbody"></tbody></table></div>
      </div>
    </div>

    <div class="pg" id="p-tarifs">
      <div class="ph"><div><h1>Tarification</h1><p>Parametres tarifaires — Admin seulement</p></div></div>
      <div class="card">
        <div class="ch"><span class="ct">Tarif actuel</span><span id="tarif-since" style="font-size:11px;color:var(--g4)"></span></div>
        <div class="cb">
          <div class="fg">
            <div class="fl"><label>Tarif par m3 (FCFA)</label><input type="number" id="t-m3" step="0.01"></div>
            <div class="fl"><label>Frais abonnement (FCFA)</label><input type="number" id="t-abo" step="0.01"></div>
            <div class="fl"><label>Frais de coupure (FCFA)</label><input type="number" id="t-coup" step="0.01"></div>
            <div class="fl"><label>TVA (%)</label><input type="number" id="t-tva" step="0.01"></div>
            <div class="fl"><label>Redevance (%)</label><input type="number" id="t-red" step="0.01"></div>
            <div class="fl"><label>Delai paiement (jours)</label><input type="number" id="t-del"></div>
            <div class="fl"><label>Seuil avant coupure (jours)</label><input type="number" id="t-seu"></div>
          </div>
          <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap">
            <button class="btn bp" id="btn-tarif" onclick="saveTarif()">Enregistrer</button>
            <div class="si">Les nouveaux tarifs s'appliquent aux prochaines factures</div>
          </div>
        </div>
      </div>
    </div>

  </div>
  <div id="footer">""" + FOOTER_SVG + """<div class="ft-cp">Administration &mdash; &copy; 2026 Sen'Eau</div><button class="ft-nv" onclick="location.href='tel:800001111'">📞 N° Vert &nbsp; 800 00 11 11</button></div>
</div>
</div>

<!-- MODALS ADMIN -->
<div class="ov" id="m-client"><div class="modal"><div class="mh"><span class="mt">Nouveau client</span><button class="mx" onclick="cm('m-client')">x</button></div>
  <div class="mb"><div class="fg">
    <div class="fl"><label>Nom *</label><input id="c-nom"></div><div class="fl"><label>Prenom *</label><input id="c-prenom"></div>
    <div class="fl"><label>Telephone</label><input id="c-tel" type="tel"></div><div class="fl"><label>Email</label><input id="c-email" type="email"></div>
    <div class="fl full"><label>Adresse *</label><textarea id="c-adr"></textarea></div>
    <div class="fl"><label>Zone *</label><select id="c-zone"><option value="">Selectionner...</option></select></div>
    <div class="fl"><label>Type</label><select id="c-type"><option value="particulier">Particulier</option><option value="entreprise">Entreprise</option><option value="administration">Administration</option></select></div>
    <div class="fl"><label>N Compteur *</label><input id="c-cpt" placeholder="ex: N0300600"></div>
    <div class="fl"><label>Index initial (m3)</label><input id="c-idx" type="number" value="0" step="0.001"></div>
  </div></div>
  <div class="mf"><button class="btn bs" onclick="cm('m-client')">Annuler</button><button class="btn bp" onclick="saveClient()">Enregistrer</button></div>
</div></div>

<div class="ov" id="m-tache"><div class="modal"><div class="mh"><span class="mt">Creer une tache</span><button class="mx" onclick="cm('m-tache')">x</button></div>
  <div class="mb"><div class="fg">
    <div class="fl full"><label>Nom *</label><input id="t-nom"></div>
    <div class="fl"><label>Type *</label><select id="t-type"><option value="releve">Releve</option><option value="coupure">Coupure</option><option value="reconnexion">Reconnexion</option><option value="maintenance">Maintenance</option></select></div>
    <div class="fl"><label>Zone</label><select id="t-zone"><option value="">Toutes zones</option></select></div>
    <div class="fl"><label>Agent affecte</label><select id="t-agent"><option value="">Non affecte</option></select></div>
    <div class="fl"><label>Date debut</label><input id="t-deb" type="date"></div>
    <div class="fl"><label>Date fin</label><input id="t-fin" type="date"></div>
    <div class="fl full"><label>Description</label><textarea id="t-desc"></textarea></div>
  </div></div>
  <div class="mf"><button class="btn bs" onclick="cm('m-tache')">Annuler</button><button class="btn bp" onclick="saveTache()">Creer</button></div>
</div></div>

<div class="ov" id="m-gfact"><div class="modal"><div class="mh"><span class="mt">Generer une facture</span><button class="mx" onclick="cm('m-gfact')">x</button></div>
  <div class="mb"><div class="fg">
    <div class="fl full"><label>Client *</label><select id="gf-cl"><option value="">Selectionner...</option></select></div>
    <div class="fl"><label>Periode debut</label><input id="gf-deb" type="date"></div>
    <div class="fl"><label>Periode fin</label><input id="gf-fin" type="date"></div>
    <div class="fl"><label>Volume consomme (m3) *</label><input id="gf-vol" type="number" step="0.001" oninput="calcFact()"></div>
    <div class="fl"><label>Frais de coupure</label><select id="gf-coup" onchange="calcFact()"><option value="0">Non</option><option value="1">Oui — 5 000 FCFA</option></select></div>
  </div>
  <div id="fact-preview" style="display:none;margin-top:14px;padding:14px;background:var(--gl);border-radius:4px;border-left:4px solid var(--gn)">
    <div style="font-size:11px;font-weight:700;color:var(--nv);margin-bottom:8px;text-transform:uppercase">Apercu du calcul</div>
    <div id="fact-lines" style="font-size:13px;display:flex;flex-direction:column;gap:5px"></div>
  </div></div>
  <div class="mf"><button class="btn bs" onclick="cm('m-gfact')">Annuler</button><button class="btn bp" onclick="saveFacture()">Generer</button></div>
</div></div>

<div class="ov" id="m-pdf"><div class="modal lg"><div class="mh"><span class="mt">Apercu Facture SEN'EAU</span><button class="mx" onclick="cm('m-pdf')">x</button></div>
  <div class="mb" id="pdf-body"></div>
  <div class="mf"><button class="btn bs" onclick="cm('m-pdf')">Fermer</button><button class="btn bp" id="pdf-dl-btn">Telecharger PDF</button></div>
</div></div>

<div class="ov" id="m-notif"><div class="modal" style="width:400px"><div class="mh"><span class="mt">Notifications</span><button class="mx" onclick="cm('m-notif')">x</button></div>
  <div class="mb" id="notif-body"></div>
  <div class="mf"><button class="btn bs" onclick="cm('m-notif')">Fermer</button></div>
</div></div>

<div id="toast"></div>
<script>
const api=async(url,o={})=>{const r=await fetch(url,{headers:{'Content-Type':'application/json'},...o});if(r.status===401){location='/login';return {error:'401'};}return r.json();};
const get=u=>api(u);
const post=(u,d)=>api(u,{method:'POST',body:JSON.stringify(d)});
const del=u=>api(u,{method:'DELETE'});
const patch=(u,d)=>api(u,{method:'PATCH',body:JSON.stringify(d)});
const fmt=n=>new Intl.NumberFormat('fr-FR').format(Math.round(n||0))+' FCFA';
const fmtm=n=>(parseFloat(n||0)).toFixed(3)+' m3';
function toast(m,t='ok'){const d=document.createElement('div');d.className='tm '+t;d.textContent=m;document.getElementById('toast').appendChild(d);setTimeout(()=>d.remove(),3500);}
function om(id){document.getElementById(id).classList.add('op');}
function cm(id){document.getElementById(id).classList.remove('op');}
document.querySelectorAll('.ov').forEach(o=>o.addEventListener('click',e=>{if(e.target===o)o.classList.remove('op');}));

let ME={role:'admin'};
const TITLES={dashboard:'Tableau de bord',clients:'Clients & Compteurs',taches:'Taches de terrain',releves:'Releves',factures:'Facturation',coupures:'Coupures & Reconnexions',signalements:'Signalements',idees:'Boite a idees',journal:"Journal d'activite",tarifs:'Tarification'};
const LOADS={dashboard:loadDash,clients:loadClients,taches:loadTaches,releves:()=>{loadReleves();document.getElementById('r-date').value=new Date().toISOString().split('T')[0];},factures:loadFactures,coupures:loadCoupures,signalements:loadSigns,idees:loadIdees,journal:loadJournal,tarifs:loadTarifs};
function nav(p,el){
  document.querySelectorAll('.pg').forEach(x=>x.classList.remove('on'));
  document.getElementById('p-'+p).classList.add('on');
  document.querySelectorAll('.ni').forEach(n=>n.classList.remove('on'));
  if(el)el.classList.add('on');
  document.getElementById('h-title').innerHTML=TITLES[p]+'<span class="hsub" id="h-sub"></span>';
  setHSub();if(LOADS[p])LOADS[p]();return false;
}
function setHSub(){const s=document.getElementById('h-sub');if(s)s.textContent=' — '+new Date().toLocaleDateString('fr-FR',{day:'numeric',month:'long',year:'numeric'});}
async function updateBadges(){const t=await get('/api/stats');document.getElementById('nb-t').textContent=t.taches_actives||0;document.getElementById('nb-f').textContent=t.factures_impayees||0;}

function tBadge(t){const m={releve:'bg-bl Releve',coupure:'bg-rd Coupure',reconnexion:'bg-lm Reconnexion',maintenance:'bg-am Maintenance'};const[c,l]=(m[t]||'bg-gr '+t).split(' ');return`<span class="badge ${c}">${l}</span>`;}
function sBadge2(s){const m={non_demarree:'bg-gr Non demarree',en_cours:'bg-bl En cours',completee:'bg-lm Completee',validee:'bg-gn Validee',rejetee:'bg-rd Rejetee'};const[c,...l]=(m[s]||'bg-gr '+s).split(' ');return`<span class="badge ${c}">${l.join(' ')}</span>`;}
function fBadge(s){const m={emise:'<span class="badge bg-bl"><span class="dot db"></span>Emise</span>',payee:'<span class="badge bg-gn"><span class="dot dg"></span>Payee</span>',depassement_delai:'<span class="badge bg-am"><span class="dot da"></span>En retard</span>'};return m[s]||`<span class="badge bg-gr">${s}</span>`;}
function cBadge(s){const m={actif:'<span class="badge bg-gn"><span class="dot dg"></span>Actif</span>',coupe:'<span class="badge bg-rd"><span class="dot dr"></span>Coupe</span>',suspendu:'<span class="badge bg-am"><span class="dot da"></span>Suspendu</span>'};return m[s]||`<span class="badge bg-gr">${s}</span>`;}
function uBadge(u){const m={faible:'bg-bl',moyen:'bg-am',eleve:'bg-rd',critique:'bg-rd'};return`<span class="badge ${m[u]||'bg-gr'}">${u}</span>`;}

async function initAdmin(){
  const me=await get('/api/me');ME=me;
  document.getElementById('ad-av').textContent=(me.prenom[0]+me.nom[0]).toUpperCase();
  document.getElementById('ad-nm').textContent=me.prenom+' '+me.nom;
  document.getElementById('ad-rl').textContent=me.email||me.login;
  const rb=document.getElementById('ad-rb');
  if(me.role==='admin'){rb.className='rbadge rb-admin';rb.textContent='Administrateur';}
  else{rb.className='rbadge rb-sup';rb.textContent='Superviseur';}
  if(me.role!=='admin'){
    document.getElementById('ni-tarif').style.display='none';
    document.getElementById('btn-tarif').disabled=true;
    document.getElementById('btn-tarif').title='Acces admin uniquement';
  }
}

async function populateSelects(){
  const zones=await get('/api/zones'),agents=await get('/api/agents'),clients=await get('/api/clients'),cpts=await get('/api/compteurs');
  ['c-zone','t-zone'].forEach(id=>{const el=document.getElementById(id);if(!el)return;const b=id==='t-zone'?'<option value="">Toutes zones</option>':'<option value="">Selectionner...</option>';el.innerHTML=b+zones.map(z=>`<option value="${z.id}">${z.nom}</option>`).join('');});
  document.getElementById('cl-z').innerHTML='<option value="">Toutes zones</option>'+zones.map(z=>`<option value="${z.id}">${z.nom}</option>`).join('');
  document.getElementById('t-agent').innerHTML='<option value="">Non affecte</option>'+agents.map(a=>`<option value="${a.id}">${a.prenom} ${a.nom}</option>`).join('');
  document.getElementById('r-cpt').innerHTML='<option value="">Selectionner...</option>'+cpts.map(c=>`<option value="${c.id}">${c.numero_compteur} — ${c.client}</option>`).join('');
  document.getElementById('gf-cl').innerHTML='<option value="">Selectionner...</option>'+clients.map(c=>`<option value="${c.id}">${c.prenom} ${c.nom} (${c.numero_compte})</option>`).join('');
}

async function loadDash(){
  const d=await get('/api/dashboard');
  document.getElementById('dash-sub').textContent='Donnees en temps reel — '+new Date().toLocaleDateString('fr-FR',{weekday:'long',year:'numeric',month:'long',day:'numeric'});
  document.getElementById('dash-stats').innerHTML=`
    <div class="sc bl"><div class="sc-ico">💧</div><div class="sc-val">${d.clients_actifs}</div><div class="sc-lbl">Clients actifs</div></div>
    <div class="sc"><div class="sc-ico">✓</div><div class="sc-val">${d.factures_payees}</div><div class="sc-lbl">Factures payees</div><div class="sc-delta up">Taux ${d.taux_collecte}%</div></div>
    <div class="sc am"><div class="sc-ico">!</div><div class="sc-val">${d.factures_impayees}</div><div class="sc-lbl">Factures impayes</div></div>
    <div class="sc rd"><div class="sc-ico">✂</div><div class="sc-val">${d.coupures_actives}</div><div class="sc-lbl">Coupures actives</div></div>`;
  const vols=d.consommations||[2800,3600,4100,3900,4300,4800],ms=d.mois_labels||['Nov','Dec','Jan','Fev','Mar','Avr'],mx=Math.max(...vols,1);
  document.getElementById('barchart').innerHTML=ms.map((m,i)=>{const h=Math.max(6,Math.round(vols[i]/mx*85));return`<div class="bw"><div class="bv">${(vols[i]/1000).toFixed(1)}k</div><div class="bar ${i===ms.length-1?'cur':''}" style="height:${h}px"></div><div class="bl">${m}</div></div>`;}).join('');
  const rv=d.releves_valides||0,rt=d.releves_total||1,pct=d.taux_collecte||0;
  document.getElementById('dash-prog').innerHTML=`
    <div><div style="display:flex;justify-content:space-between;font-size:12px;color:var(--g4);margin-bottom:3px"><span>Releves valides</span><span style="color:var(--gd)">${rv}/${rt}</span></div><div class="pb"><div class="pf g" style="width:${rt?Math.round(rv/rt*100):0}%"></div></div></div>
    <div><div style="display:flex;justify-content:space-between;font-size:12px;color:var(--g4);margin-bottom:3px"><span>Taux recouvrement</span><span style="color:var(--nv)">${pct}%</span></div><div class="pb"><div class="pf b" style="width:${pct}%"></div></div></div>`;
  document.getElementById('dash-tl').innerHTML=(d.activites||[]).map(a=>`<div class="tli"><div class="tld ${a.c}"></div><div class="tli-t">${a.t}</div><div class="tli-d">${a.d}</div></div>`).join('');
  document.getElementById('dash-alerts').innerHTML=d.factures_impayees>0?`<div class="alert al-a"><span>!</span><div><strong>${d.factures_impayees} factures impayes</strong> — dont ${d.en_retard} en retard.</div></div>`:'';
  document.getElementById('dash-taches').innerHTML=(d.taches||[]).map(t=>`<tr><td><strong>${t.nom}</strong></td><td>${tBadge(t.type)}</td><td>${t.zone||'—'}</td><td>${t.agent||'—'}</td><td>${sBadge2(t.statut)}</td></tr>`).join('')||'<tr><td colspan="5" class="er">Aucune tache</td></tr>';
}

async function loadClients(){
  const s=document.getElementById('cl-s').value,z=document.getElementById('cl-z').value,st=document.getElementById('cl-st').value;
  const d=await get(`/api/clients?s=${encodeURIComponent(s)}&zone=${z}&statut=${st}`);
  const stats=await get('/api/clients/stats');
  document.getElementById('cl-stats').innerHTML=`
    <div class="sc bl"><div class="sc-ico">👤</div><div class="sc-val">${stats.total}</div><div class="sc-lbl">Total clients</div></div>
    <div class="sc"><div class="sc-ico">💧</div><div class="sc-val">${stats.actifs}</div><div class="sc-lbl">Actifs</div></div>
    <div class="sc rd"><div class="sc-ico">✂</div><div class="sc-val">${stats.coupes}</div><div class="sc-lbl">Coupes</div></div>
    <div class="sc am"><div class="sc-ico">~</div><div class="sc-val">${stats.suspendus}</div><div class="sc-lbl">Suspendus</div></div>`;
  document.getElementById('cl-n').textContent=d.length+' client(s)';
  document.getElementById('cl-tbody').innerHTML=d.length?d.map(r=>`<tr>
    <td><span class="mono">${r.numero_compte||'—'}</span></td>
    <td><strong>${r.nom} ${r.prenom}</strong><div style="font-size:11px;color:var(--g4)">${r.adresse||''}</div></td>
    <td>${r.zone||'—'}</td>
    <td><span class="mono">${r.compteur||'—'}</span></td>
    <td style="font-size:11px">${r.dernier_relevage||'—'}</td>
    <td>${cBadge(r.statut_compteur)}</td>
    <td style="display:flex;gap:5px;flex-wrap:wrap">
      <button class="btn bg bsm" onclick="openGF(${r.id})">Facture</button>
      <button class="btn bs bsm" onclick="delClient(${r.id})" style="color:var(--rd)">Suppr.</button>
    </td>
  </tr>`).join(''):'<tr><td colspan="7" class="er">Aucun client</td></tr>';
}
async function delClient(id){if(!confirm('Supprimer ce client et son compteur ?'))return;const r=await del(`/api/clients/${id}`);if(r.error){toast(r.error,'err');return;}toast('Client supprime','inf');loadClients();updateBadges();}
async function saveClient(){
  const nom=document.getElementById('c-nom').value.trim(),pr=document.getElementById('c-prenom').value.trim(),adr=document.getElementById('c-adr').value.trim(),z=document.getElementById('c-zone').value,cpt=document.getElementById('c-cpt').value.trim();
  if(!nom||!pr||!adr||!z||!cpt){toast('Champs * requis','err');return;}
  const r=await post('/api/clients',{nom,prenom:pr,adresse:adr,telephone:document.getElementById('c-tel').value,email:document.getElementById('c-email').value,type_client:document.getElementById('c-type').value,id_zone:z,numero_compteur:cpt,index_initial:parseFloat(document.getElementById('c-idx').value)||0});
  if(r.error){toast(r.error,'err');return;}
  cm('m-client');toast('Client enregistre','ok');
  ['c-nom','c-prenom','c-tel','c-email','c-adr','c-cpt'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('c-idx').value='0';loadClients();updateBadges();populateSelects();
}

let tF='';
async function loadTaches(){
  const d=await get('/api/taches?type='+tF);
  document.getElementById('ta-tbody').innerHTML=d.length?d.map(t=>`<tr>
    <td><strong>${t.nom}</strong></td><td>${tBadge(t.type)}</td>
    <td>${t.zone||'—'}</td>
    <td>${t.agent||'<span style="color:var(--g4)">Non affecte</span>'}</td>
    <td style="font-size:11px">${t.date_debut_prevue||'—'} → ${t.date_fin_prevue||'—'}</td>
    <td>${sBadge2(t.statut)}</td>
    <td style="display:flex;gap:5px">
      ${t.statut==='non_demarree'?`<button class="btn bg bsm" onclick="majT(${t.id},'en_cours')">Demarrer</button>`:''}
      ${t.statut==='en_cours'?`<button class="btn bp bsm" onclick="majT(${t.id},'completee')">Terminer</button>`:''}
      <button class="btn bs bsm" onclick="delT(${t.id})" style="color:var(--rd)">Suppr.</button>
    </td>
  </tr>`).join(''):'<tr><td colspan="7" class="er">Aucune tache</td></tr>';
}
function filterT(t,el){tF=t;document.querySelectorAll('.tag').forEach(x=>x.classList.remove('on'));el.classList.add('on');loadTaches();}
async function majT(id,s){await patch(`/api/taches/${id}`,{statut:s});toast('Statut mis a jour','ok');loadTaches();updateBadges();}
async function delT(id){if(!confirm('Supprimer ?'))return;await del(`/api/taches/${id}`);toast('Supprime','inf');loadTaches();updateBadges();}
async function saveTache(){
  const nom=document.getElementById('t-nom').value.trim();if(!nom){toast('Nom requis','err');return;}
  const r=await post('/api/taches',{nom,type:document.getElementById('t-type').value,id_zone:document.getElementById('t-zone').value||null,id_agent:document.getElementById('t-agent').value||null,date_debut_prevue:document.getElementById('t-deb').value||null,date_fin_prevue:document.getElementById('t-fin').value||null,description:document.getElementById('t-desc').value});
  if(r.error){toast(r.error,'err');return;}
  cm('m-tache');toast('Tache creee','ok');['t-nom','t-desc'].forEach(id=>document.getElementById(id).value='');loadTaches();updateBadges();
}

async function loadReleves(){
  const d=await get('/api/releves');
  document.getElementById('rel-tbody').innerHTML=d.length?d.map(r=>`<tr>
    <td><span class="mono">${r.compteur}</span><div style="font-size:11px;color:var(--g4)">${r.client||''}</div></td>
    <td style="font-size:11px">${r.date_releve}</td>
    <td style="font-weight:600;color:${r.volume_m3>30?'var(--am)':'var(--nv)'}">${fmtm(r.volume_m3)}</td>
    <td>${r.statut_validation==='valide'?'<span class="badge bg-gn">Valide</span>':r.statut_validation==='rejete'?'<span class="badge bg-rd">Rejete</span>':`<div style="display:flex;gap:4px"><button class="btn bg bsm" onclick="valR(${r.id})">Val.</button><button class="btn br2 bsm" onclick="rejR(${r.id})">Rej.</button></div>`}</td>
  </tr>`).join(''):'<tr><td colspan="4" class="er">Aucun releve</td></tr>';
}
document.getElementById('r-cpt').addEventListener('change',async function(){
  if(!this.value){document.getElementById('r-info').innerHTML='';return;}
  const c=await get('/api/compteur/'+this.value);
  if(c&&!c.error)document.getElementById('r-info').innerHTML=`<div class="alert al-g" style="margin:0;font-size:12px">Client : <strong>${c.client}</strong> — Dernier index : <strong>${fmtm(c.dernier_index)}</strong></div>`;
});
async function saveReleve(){
  const id=document.getElementById('r-cpt').value,date=document.getElementById('r-date').value,idx=document.getElementById('r-idx').value;
  if(!id||!date||!idx){toast('Champs * requis','err');return;}
  const r=await post('/api/releves',{id_compteur:id,date_releve:date,index_compteur:parseFloat(idx),etat_compteur:document.getElementById('r-etat').value,commentaires:document.getElementById('r-comm').value});
  if(r.error){toast(r.error,'err');return;}
  toast('Releve enregistre — '+fmtm(r.volume),'ok');
  document.getElementById('r-idx').value='';document.getElementById('r-comm').value='';document.getElementById('r-info').innerHTML='';loadReleves();
}
async function valR(id){const r=await patch(`/api/releves/${id}`,{statut:'valide'});if(r.error){toast(r.error,'err');return;}toast('Valide','ok');loadReleves();}
async function rejR(id){const r=await patch(`/api/releves/${id}`,{statut:'rejete'});if(r.error){toast(r.error,'err');return;}toast('Rejete','inf');loadReleves();}

async function loadFactures(){
  const st=document.getElementById('f-st').value,mo=document.getElementById('f-mo').value;
  const d=await get(`/api/factures?statut=${st}&mois=${mo}`);
  if(d.error){toast(d.error,'err');return;}
  const s=await get('/api/factures/stats');
  document.getElementById('fact-stats').innerHTML=`
    <div class="sc bl"><div class="sc-ico">📄</div><div class="sc-val">${s.total}</div><div class="sc-lbl">Total</div></div>
    <div class="sc"><div class="sc-ico">✓</div><div class="sc-val">${s.payees}</div><div class="sc-lbl">Payees</div></div>
    <div class="sc am"><div class="sc-ico">!</div><div class="sc-val">${s.impayees}</div><div class="sc-lbl">Impayes</div></div>
    <div class="sc"><div class="sc-ico">💰</div><div class="sc-val" style="font-size:16px">${fmt(s.total_encaisse)}</div><div class="sc-lbl">Encaisse</div></div>`;
  document.getElementById('fact-tbody').innerHTML=d.length?d.map(f=>`<tr>
    <td><span class="mono">${f.numero_facture}</span></td>
    <td><strong>${f.client}</strong></td>
    <td style="font-size:11px">${f.periode_debut} → ${f.periode_fin}</td>
    <td style="font-size:11px">${fmtm(f.volume_m3)}</td>
    <td style="font-weight:700">${fmt(f.montant_total)}</td>
    <td style="font-size:11px;${f.statut_paiement==='depassement_delai'?'color:var(--rd);font-weight:700':''}">${f.date_limite_paiement}</td>
    <td>${fBadge(f.statut_paiement)}</td>
    <td style="display:flex;gap:4px;flex-wrap:wrap">
      <button class="btn bg bsm" onclick="voirPDF(${f.id})">PDF</button>
      ${f.statut_paiement!=='payee'?`<button class="btn bp bsm" onclick="payerF(${f.id})">Paye</button>`:''}
      <button class="btn bs bsm" onclick="delF(${f.id})" style="color:var(--rd)">Suppr.</button>
    </td>
  </tr>`).join(''):'<tr><td colspan="8" class="er">Aucune facture</td></tr>';
}
async function payerF(id){await patch(`/api/factures/${id}`,{statut:'payee'});toast('Facture payee','ok');loadFactures();updateBadges();}
async function delF(id){if(!confirm('Supprimer cette facture ?'))return;const r=await del(`/api/factures/${id}`);if(r.error){toast(r.error,'err');return;}toast('Supprimee','inf');loadFactures();updateBadges();}

let TC={tarif_m3:650,frais_abonnement:2500,taxe_tva_pct:18,taxe_redevance_pct:3,frais_coupure_montant:5000};
async function calcFact(){
  const vol=parseFloat(document.getElementById('gf-vol').value)||0;
  if(!vol){document.getElementById('fact-preview').style.display='none';return;}
  const coup=document.getElementById('gf-coup').value==='1',t=TC;
  const cn=vol*t.tarif_m3,ab=t.frais_abonnement,base=cn+ab,tx=base*(t.taxe_tva_pct+t.taxe_redevance_pct)/100,cp=coup?t.frais_coupure_montant:0,tot=base+tx+cp;
  document.getElementById('fact-preview').style.display='block';
  document.getElementById('fact-lines').innerHTML=`
    <div style="display:flex;justify-content:space-between"><span>Consommation (${vol} m3)</span><strong>${fmt(cn)}</strong></div>
    <div style="display:flex;justify-content:space-between"><span>Frais abonnement</span><strong>${fmt(ab)}</strong></div>
    <div style="display:flex;justify-content:space-between;color:var(--g4)"><span>Taxes (${t.taxe_tva_pct+t.taxe_redevance_pct}%)</span><span>${fmt(tx)}</span></div>
    ${coup?`<div style="display:flex;justify-content:space-between;color:var(--rd)"><span>Frais coupure</span><strong>${fmt(cp)}</strong></div>`:''}
    <div style="display:flex;justify-content:space-between;font-size:15px;font-weight:700;border-top:2px solid var(--nv);padding-top:8px;margin-top:4px"><span>TOTAL TTC</span><span>${fmt(tot)}</span></div>`;
}
async function saveFacture(){
  const cl=document.getElementById('gf-cl').value,vol=document.getElementById('gf-vol').value,deb=document.getElementById('gf-deb').value,fin=document.getElementById('gf-fin').value;
  if(!cl||!vol||!deb||!fin){toast('Champs * requis','err');return;}
  const r=await post('/api/factures',{id_client:cl,volume_m3:parseFloat(vol),periode_debut:deb,periode_fin:fin,frais_coupure:document.getElementById('gf-coup').value==='1'});
  if(r.error){toast(r.error,'err');return;}
  cm('m-gfact');toast('Facture '+r.numero+' generee','ok');
  document.getElementById('gf-vol').value='';document.getElementById('fact-preview').style.display='none';
  loadFactures();updateBadges();
}
function openGF(cid){document.getElementById('gf-cl').value=cid;om('m-gfact');}

async function voirPDF(id){
  const f=await get('/api/factures/'+id);
  if(f.error){toast(f.error,'err');return;}
  const hist=[28,20,35,15,40,32,f.volume_m3||12],mois=['Oct','Nov','Dec','Jan','Fev','Mar','Avr'],mx=Math.max(...hist,1);
  document.getElementById('pdf-body').innerHTML=`<div class="inv">
    <div class="inv-hdr">
      <div><div class="inv-brand">SEN<span>'</span>EAU</div><div class="inv-sub">Grand Dakar — 100 Nelson Diallo, Dakar | Tel : 800 11 11</div></div>
      <div>
        <div style="font-size:17px;font-weight:700;color:var(--nv);text-align:right;font-family:'Playfair Display',serif">FACTURE</div>
        <div style="font-size:10px;color:var(--g4);text-align:right">du ${f.date_facture}</div>
        <div class="inv-box" style="margin-top:7px"><div class="lbl">Montant a regler</div><div class="amt">${fmt(f.montant_total)}</div><div class="dl">Avant le <strong>${f.date_limite_paiement}</strong></div></div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
      <div class="inv-sect"><h4>Informations client</h4>
        <div class="inv-row"><span style="color:var(--g4)">Reference</span><span class="mono">${f.client.numero_compte||'—'}</span></div>
        <div class="inv-row"><span style="color:var(--g4)">N Facture</span><span class="mono">${f.numero_facture}</span></div>
        <div class="inv-row"><span style="color:var(--g4)">N Compteur</span><span class="mono">${f.client.compteur||'—'}</span></div>
        <div class="inv-row"><span style="color:var(--g4)">Periode</span><span>${f.periode_debut} → ${f.periode_fin}</span></div>
      </div>
      <div class="inv-sect"><h4>Destinataire</h4>
        <div style="font-weight:700;font-family:'Playfair Display',serif;color:var(--nv)">${f.client.prenom} ${f.client.nom}</div>
        <div style="color:var(--g4);font-size:11px;margin-top:4px">${f.client.adresse||''}</div>
        <div style="color:var(--g4);font-size:11px">Tel : ${f.client.telephone||'—'}</div>
      </div>
    </div>
    <table class="inv-tbl"><thead><tr><th>Designation</th><th>Qte</th><th>P.U.</th><th>Montant TTC</th></tr></thead>
    <tbody>
      <tr><td>Consommation eau</td><td>${fmtm(f.volume_m3)}</td><td>${fmt(f.montant_consommation/(f.volume_m3||1))}/m3</td><td>${fmt(f.montant_consommation)}</td></tr>
      <tr><td>Frais d'abonnement</td><td>1 mois</td><td>${fmt(f.montant_abonnement)}</td><td>${fmt(f.montant_abonnement)}</td></tr>
      <tr><td>Taxes (TVA + Redevance)</td><td>—</td><td>—</td><td>${fmt(f.montant_taxes)}</td></tr>
      ${f.montant_frais_coupure>0?`<tr><td style="color:var(--rd)"><strong>Frais de coupure</strong></td><td>1</td><td>${fmt(f.montant_frais_coupure)}</td><td style="color:var(--rd);font-weight:700">${fmt(f.montant_frais_coupure)}</td></tr>`:''}
      <tr class="tr-tot"><td><strong>TOTAL TTC</strong></td><td></td><td></td><td style="font-size:14px">${fmt(f.montant_total)}</td></tr>
    </tbody></table>
    <div class="inv-sect"><h4>Historique consommation (m3)</h4>
      <div class="inv-hist">${hist.map((v,i)=>`<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="flex:1;width:75%;background:${i===hist.length-1?'var(--gn)':'#5a9a1a'};border-radius:2px 2px 0 0;height:${Math.max(4,Math.round(v/mx*46))}px"></div><span class="inv-bar-lbl">${mois[i]}</span></div>`).join('')}</div>
    </div>
    <div class="inv-total">
      <div><div style="font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px">Montant a regler</div><div class="big">${fmt(f.montant_total)}</div></div>
      <div style="text-align:right;font-size:12px"><div>Statut : <strong>${f.statut_paiement==='payee'?'PAYEE':'EN ATTENTE'}</strong></div><div style="margin-top:4px">Date limite : <strong>${f.date_limite_paiement}</strong></div></div>
    </div>
    <div class="inv-foot">
      <div>TALON A JOINDRE AU PAIEMENT — Modes : Especes · Virement · Orange Money · Wave</div>
      <div style="display:flex;justify-content:space-between;margin-top:5px;font-size:10px"><span>N : <strong>${f.numero_facture}</strong></span><span>Periode : ${f.periode_debut} → ${f.periode_fin}</span><span>TOTAL : <strong>${fmt(f.montant_total)}</strong></span></div>
      <div class="inv-barcode">||||||||||||||||||||||||||||||||||</div>
    </div>
  </div>`;
  document.getElementById('pdf-dl-btn').onclick=()=>{window.location.href='/api/factures/'+id+'/pdf';};
  om('m-pdf');
}

async function loadCoupures(){
  const d=await get('/api/coupures');
  document.getElementById('coup-tbody').innerHTML=d.length?d.map(c=>`<tr>
    <td><strong>${c.client}</strong></td>
    <td><span class="mono">${c.compteur}</span></td>
    <td>${fmt(c.montant_facture)}</td>
    <td style="color:var(--rd);font-weight:700">+ ${fmt(c.frais_coupure)}</td>
    <td style="font-weight:700">${fmt(c.total_du)}</td>
    <td>${c.statut_coupure==='effectuee'?'<span class="badge bg-rd">Coupe</span>':'<span class="badge bg-am">Programmee</span>'}</td>
    <td style="display:flex;gap:4px">
      ${c.statut_coupure==='en_attente'?`<button class="btn br2 bsm" onclick="effC(${c.id})">Couper</button>`:''}
      <button class="btn bg bsm" onclick="recon(${c.id_facture})">Reconn.</button>
    </td>
  </tr>`).join(''):'<tr><td colspan="7" class="er">Aucune coupure</td></tr>';
}
async function genCoupures(){const r=await post('/api/coupures/auto',{});if(r.error){toast(r.error,'err');return;}toast(r.created+' coupure(s) generee(s)','ok');loadCoupures();updateBadges();}
async function effC(id){const r=await patch(`/api/coupures/${id}`,{statut:'effectuee'});if(r.error){toast(r.error,'err');return;}toast('Coupure effectuee','ok');loadCoupures();}
async function recon(fId){if(!confirm('Reconnecter ce client ?'))return;await payerF(fId);await post('/api/coupures/reconnecter',{id_facture:fId});toast('Reconnecte','ok');loadCoupures();loadFactures();updateBadges();}

async function loadSigns(){
  const d=await get('/api/signalements');
  document.getElementById('sig-tbody').innerHTML=d.length?d.map(s=>`<tr>
    <td style="font-size:11px">${s.created_at?.substring(0,10)||'—'}</td>
    <td><span class="badge bg-am">${s.type_compromission}</span></td>
    <td>${s.zone||'—'}</td>
    <td>${s.contenu}</td>
    <td>${uBadge(s.urgence)}</td>
    <td>${s.traite?'<span class="badge bg-gn">Traite</span>':'<span class="badge bg-am">En cours</span>'}</td>
    <td>${!s.traite?`<button class="btn bg bsm" onclick="traiterS(${s.id})">Clore</button>`:'—'}</td>
  </tr>`).join(''):'<tr><td colspan="7" class="er">Aucun signalement</td></tr>';
}
async function traiterS(id){await patch('/api/signalements/'+id,{traite:1});toast('Clôture','ok');loadSigns();}

async function loadIdees(){
  const d=await get('/api/idees');
  const bm={soumise:'bg-bl Soumise',en_etude:'bg-am En etude',approuvee:'bg-gn Approuvee',rejetee:'bg-rd Rejetee'};
  document.getElementById('idees-list').innerHTML=d.length?d.map(i=>{
    const[c,...l]=(bm[i.statut]||'bg-gr '+i.statut).split(' ');
    return`<div class="ic"><div class="ic-h"><div><div class="ic-t">${i.titre}</div><div class="ic-m">${i.categorie||''} — ${i.created_at?.substring(0,10)||''}</div></div><span class="badge ${c}">${l.join(' ')}</span></div><div class="ic-b">${i.contenu}</div>${i.statut==='soumise'?`<div style="margin-top:10px;display:flex;gap:8px"><button class="btn bg bsm" onclick="majI(${i.id},'approuvee')">Approuver</button><button class="btn br2 bsm" onclick="majI(${i.id},'rejetee')">Rejeter</button><button class="btn bs bsm" onclick="majI(${i.id},'en_etude')">En etude</button></div>`:''}</div>`;
  }).join(''):'<div class="alert al-g">Aucune idee soumise.</div>';
}
async function majI(id,s){await patch('/api/idees/'+id,{statut:s});toast('Statut mis a jour','ok');loadIdees();}

async function loadJournal(){
  const s=document.getElementById('j-s').value.toLowerCase();
  const d=await get('/api/journal');
  if(d.error){toast(d.error,'err');return;}
  const f=s?d.filter(j=>(j.action+j.details+(j.nom||'')+(j.prenom||'')).toLowerCase().includes(s)):d;
  const rc={admin:'#7a5000',superviseur:'#3a6010',agent_releve:'#1B3A6B',agent_coupure:'#1B3A6B'};
  document.getElementById('j-tbody').innerHTML=f.length?f.map(j=>`<tr class="${j.role==='admin'?'j-admin':j.role==='superviseur'?'j-sup':''}">
    <td style="font-size:11px;white-space:nowrap">${j.created_at}</td>
    <td><strong>${j.prenom||'?'} ${j.nom||''}</strong></td>
    <td><span style="font-size:10px;font-weight:700;color:${rc[j.role]||'#666'};text-transform:uppercase">${j.role||'—'}</span></td>
    <td><span class="badge bg-gn">${j.action}</span></td>
    <td style="font-size:11px;color:var(--g4)">${j.details||''}</td>
  </tr>`).join(''):'<tr><td colspan="5" class="er">Aucune activite enregistree</td></tr>';
}

async function loadTarifs(){
  const t=await get('/api/tarifs');TC=t;
  document.getElementById('tarif-since').textContent='En vigueur depuis le '+t.date_debut;
  [['t-m3','tarif_m3'],['t-abo','frais_abonnement'],['t-coup','frais_coupure_montant'],['t-tva','taxe_tva_pct'],['t-red','taxe_redevance_pct'],['t-del','delai_paiement_jours'],['t-seu','seuil_impayement_jours']].forEach(([id,k])=>document.getElementById(id).value=t[k]);
}
async function saveTarif(){
  const r=await post('/api/tarifs',{tarif_m3:document.getElementById('t-m3').value,frais_abonnement:document.getElementById('t-abo').value,frais_coupure_montant:document.getElementById('t-coup').value,taxe_tva_pct:document.getElementById('t-tva').value,taxe_redevance_pct:document.getElementById('t-red').value,delai_paiement_jours:document.getElementById('t-del').value,seuil_impayement_jours:document.getElementById('t-seu').value});
  if(r.error){toast(r.error,'err');return;}TC=r;toast('Tarif mis a jour','ok');
}

async function openNotif(){
  const d=await get('/api/notifications');
  document.getElementById('notif-body').innerHTML=d.length?d.map(n=>`<div style="padding:11px;border-radius:4px;${n.bg};margin-bottom:8px"><div style="font-size:13px;font-weight:600">${n.titre}</div><div style="font-size:11px;color:var(--g4);margin-top:2px">${n.detail}</div></div>`).join(''):'<div style="padding:16px;text-align:center;color:var(--g4)">Aucune notification</div>';
  om('m-notif');
}

window.addEventListener('DOMContentLoaded',async()=>{
  setHSub();await initAdmin();await populateSelects();
  await loadTarifs().catch(()=>{});await loadDash();await updateBadges();
});
</script>
</body></html>
"""

# ═══════════════════════════════════════════════════════════
#  ROUTES FLASK
# ═══════════════════════════════════════════════════════════

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        lg = request.form.get('login','').strip()
        pw = request.form.get('password','')
        db = get_db()
        u = db.execute("SELECT * FROM utilisateur WHERE login=? AND statut='actif'", (lg,)).fetchone()
        db.close()
        if u and u['password_hash'] == hp(pw):
            session['uid'] = u['id']
            log_action('Connexion', f"login:{lg}")
            return redirect('/')
        return render_template_string(LOGIN_HTML, error="Identifiant ou mot de passe incorrect.")
    return render_template_string(LOGIN_HTML, error=None)

@app.route('/logout')
def logout():
    log_action('Deconnexion','')
    session.clear()
    return redirect('/login')

@app.route('/')
def index():
    u = cur_user()
    if not u: return redirect('/login')
    if u['role'] in ('admin','superviseur'): return render_template_string(ADMIN_HTML)
    return render_template_string(AGENT_HTML)

@app.route('/api/me')
@login_required
def api_me():
    u = cur_user()
    db = get_db()
    z = db.execute("SELECT nom FROM zone WHERE id=?", (u['id_zone'],)).fetchone() if u['id_zone'] else None
    db.close()
    return jsonify(id=u['id'], nom=u['nom'], prenom=u['prenom'], role=u['role'],
                   id_zone=u['id_zone'], zone=z['nom'] if z else 'Toutes zones',
                   email=u.get('email',''), login=u.get('login',''))

@app.route('/api/stats')
@login_required
def api_stats():
    db = get_db()
    ta = db.execute("SELECT COUNT(*) FROM tache WHERE statut NOT IN ('completee','validee','annulee')").fetchone()[0]
    fi = db.execute("SELECT COUNT(*) FROM facture WHERE statut_paiement NOT IN ('payee','annulee')").fetchone()[0]
    db.close(); return jsonify(taches_actives=ta, factures_impayees=fi)

@app.route('/api/dashboard')
@login_required
def api_dashboard():
    db = get_db()
    ca = db.execute("SELECT COUNT(*) FROM client WHERE statut_client='actif'").fetchone()[0]
    fp = db.execute("SELECT COUNT(*) FROM facture WHERE statut_paiement='payee'").fetchone()[0]
    fi = db.execute("SELECT COUNT(*) FROM facture WHERE statut_paiement NOT IN ('payee','annulee')").fetchone()[0]
    er = db.execute("SELECT COUNT(*) FROM facture WHERE statut_paiement='depassement_delai'").fetchone()[0]
    cc = db.execute("SELECT COUNT(*) FROM coupure WHERE statut_coupure IN ('en_attente','effectuee')").fetchone()[0]
    rv = db.execute("SELECT COUNT(*) FROM releve WHERE statut_validation='valide'").fetchone()[0]
    rt = db.execute("SELECT COUNT(*) FROM releve").fetchone()[0]
    tot = max(fp+fi,1); pct = round(fp/tot*100)
    taches = db.execute("""SELECT t.nom,t.type,t.statut,z.nom zone,u.prenom||' '||u.nom agent
        FROM tache t LEFT JOIN zone z ON t.id_zone=z.id LEFT JOIN utilisateur u ON t.id_agent=u.id
        WHERE t.statut NOT IN ('completee','validee') LIMIT 5""").fetchall()
    db.close()
    return jsonify(clients_actifs=ca, factures_payees=fp, factures_impayees=fi, en_retard=er,
        coupures_actives=cc, releves_valides=rv, releves_total=rt, taux_collecte=pct,
        activites=[{"c":"b","t":"Systeme operationnel","d":"Maintenant"},{"c":"g","t":f"{fp} factures payees","d":"Total"},{"c":"a","t":f"{fi} factures impayes","d":"A traiter"},{"c":"r","t":f"{cc} coupures actives","d":"Urgent"}],
        consommations=[2800,3600,4100,3900,4300,rv*3 or 4800], mois_labels=['Nov','Dec','Jan','Fev','Mar','Avr'],
        taches=[dict(r) for r in taches])

# ── AGENT ROUTES ─────────────────────────────────────────────
@app.route('/api/agent/dashboard')
@login_required
def api_agent_dashboard():
    u = cur_user(); z = u['id_zone']; db = get_db()
    mt = db.execute("SELECT COUNT(*) FROM tache WHERE id_agent=? AND statut NOT IN ('completee','annulee')", (u['id'],)).fetchone()[0]
    mr = db.execute("SELECT COUNT(*) FROM releve r JOIN compteur c ON r.id_compteur=c.id WHERE strftime('%Y-%m',r.date_releve)=strftime('%Y-%m','now') AND (? IS NULL OR c.id_zone=?)", (z,z)).fetchone()[0]
    ra = db.execute("SELECT COUNT(*) FROM releve r JOIN compteur c ON r.id_compteur=c.id WHERE r.statut_validation='en_attente' AND (? IS NULL OR c.id_zone=?)", (z,z)).fetchone()[0]
    taches = db.execute("SELECT t.nom,t.type,t.statut FROM tache t WHERE t.id_agent=? AND t.statut NOT IN ('completee','annulee') LIMIT 5", (u['id'],)).fetchall()
    dern = db.execute("SELECT r.*,c.numero_compteur compteur FROM releve r JOIN compteur c ON r.id_compteur=c.id WHERE (? IS NULL OR c.id_zone=?) ORDER BY r.created_at DESC LIMIT 5", (z,z)).fetchall()
    db.close()
    return jsonify(mes_taches=mt, mes_releves=mr, releves_attente=ra,
        taches=[dict(r) for r in taches], derniers_releves=[dict(r) for r in dern])

@app.route('/api/agent/taches')
@login_required
def api_agent_taches():
    u = cur_user(); db = get_db()
    rows = db.execute("SELECT t.*,z.nom zone FROM tache t LEFT JOIN zone z ON t.id_zone=z.id WHERE t.id_agent=? ORDER BY t.created_at DESC", (u['id'],)).fetchall()
    db.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/agent/releves')
@login_required
def api_agent_releves():
    u = cur_user(); z = u['id_zone']; db = get_db()
    rows = db.execute("SELECT r.*,c.numero_compteur compteur,cl.nom||' '||cl.prenom client FROM releve r JOIN compteur c ON r.id_compteur=c.id JOIN client cl ON c.id_client=cl.id WHERE (? IS NULL OR c.id_zone=?) ORDER BY r.created_at DESC LIMIT 30", (z,z)).fetchall()
    db.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/agent/clients')
@login_required
def api_agent_clients():
    u = cur_user(); z = u['id_zone']; s = request.args.get('s','').lower(); db = get_db()
    rows = db.execute("SELECT c.*,co.numero_compteur compteur,co.statut_compteur,co.dernier_relevage FROM client c LEFT JOIN compteur co ON co.id_client=c.id WHERE (? IS NULL OR c.id_zone=?)", (z,z)).fetchall()
    db.close()
    result = [dict(r) for r in rows]
    if s: result = [r for r in result if s in (r.get('nom','')+r.get('prenom','')+str(r.get('numero_compte',''))+str(r.get('compteur',''))).lower()]
    return jsonify(result)

@app.route('/api/agent/factures')
@login_required
def api_agent_factures():
    u = cur_user(); z = u['id_zone']; db = get_db()
    rows = db.execute("SELECT f.*,c.nom||' '||c.prenom client FROM facture f JOIN client c ON f.id_client=c.id WHERE (? IS NULL OR c.id_zone=?) ORDER BY f.date_facture DESC", (z,z)).fetchall()
    db.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/agent/signalements')
@login_required
def api_agent_signalements():
    u = cur_user(); z = u['id_zone']; db = get_db()
    rows = db.execute("SELECT * FROM signalement WHERE (? IS NULL OR id_zone=?) ORDER BY created_at DESC", (z,z)).fetchall()
    db.close(); return jsonify([dict(r) for r in rows])

# ── GENERAL ──────────────────────────────────────────────────
@app.route('/api/zones')
@login_required
def api_zones():
    db = get_db(); rows = db.execute("SELECT id,nom FROM zone ORDER BY nom").fetchall(); db.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/agents')
@login_required
def api_agents():
    db = get_db(); rows = db.execute("SELECT id,nom,prenom FROM utilisateur ORDER BY prenom").fetchall(); db.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/compteurs')
@login_required
def api_compteurs():
    u = cur_user(); z = u['id_zone']; db = get_db()
    rows = db.execute("SELECT co.*,cl.nom||' '||cl.prenom client FROM compteur co JOIN client cl ON co.id_client=cl.id WHERE (? IS NULL OR co.id_zone=?) ORDER BY co.numero_compteur", (z,z)).fetchall()
    db.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/clients')
@login_required
def api_clients():
    db = get_db()
    s = request.args.get('s','').lower(); zone = request.args.get('zone',''); st = request.args.get('statut','')
    sql = "SELECT c.*,z.nom zone,co.numero_compteur compteur,co.statut_compteur,co.dernier_relevage,co.id compteur_id FROM client c LEFT JOIN zone z ON c.id_zone=z.id LEFT JOIN compteur co ON co.id_client=c.id WHERE 1=1"
    params = []
    if zone: sql += " AND c.id_zone=?"; params.append(zone)
    if st: sql += " AND co.statut_compteur=?"; params.append(st)
    rows = db.execute(sql, params).fetchall(); db.close()
    result = [dict(r) for r in rows]
    if s: result = [r for r in result if s in (r.get('nom','')+r.get('prenom','')+(r.get('numero_compte') or '')+(r.get('compteur') or '')).lower()]
    return jsonify(result)

@app.route('/api/clients/stats')
@login_required
def api_clients_stats():
    db = get_db()
    t = db.execute("SELECT COUNT(*) FROM client").fetchone()[0]
    a = db.execute("SELECT COUNT(*) FROM compteur WHERE statut_compteur='actif'").fetchone()[0]
    c = db.execute("SELECT COUNT(*) FROM compteur WHERE statut_compteur='coupe'").fetchone()[0]
    s = db.execute("SELECT COUNT(*) FROM compteur WHERE statut_compteur='suspendu'").fetchone()[0]
    db.close(); return jsonify(total=t, actifs=a, coupes=c, suspendus=s)

@app.route('/api/clients', methods=['POST'])
@login_required
def api_clients_post():
    if not is_staff(): return jsonify(error='Acces refuse — staff requis'), 403
    d = request.json
    if not all([d.get('nom'), d.get('prenom'), d.get('adresse'), d.get('id_zone'), d.get('numero_compteur')]):
        return jsonify(error='Champs obligatoires manquants'), 400
    compte = f"SN-{str(abs(hash(d['nom']+d['prenom'])))[-4:]}-{str(abs(hash(d.get('email','x'))))[-4:]}"
    db = get_db()
    try:
        db.execute("INSERT INTO client(nom,prenom,adresse,telephone,email,numero_compte,type_client,statut_client,id_zone) VALUES(?,?,?,?,?,?,?,?,?)", (d['nom'],d['prenom'],d['adresse'],d.get('telephone',''),d.get('email',''),compte,d.get('type_client','particulier'),'actif',d['id_zone']))
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO compteur(numero_compteur,id_client,id_zone,statut_compteur,dernier_index) VALUES(?,?,?,?,?)", (d['numero_compteur'],cid,d['id_zone'],'actif',d.get('index_initial',0)))
        db.commit()
    except Exception as e: return jsonify(error=str(e)), 400
    finally: db.close()
    log_action('Nouveau client', f"{d['nom']} {d['prenom']} — {compte}")
    return jsonify(ok=True, compte=compte)

@app.route('/api/clients/<int:cid>', methods=['DELETE'])
@login_required
def api_client_del(cid):
    if not is_admin(): return jsonify(error='Acces refuse — admin requis'), 403
    db = get_db()
    db.execute("DELETE FROM compteur WHERE id_client=?", (cid,))
    db.execute("DELETE FROM client WHERE id=?", (cid,))
    db.commit(); db.close()
    log_action('Suppression client', f"ID:{cid}"); return jsonify(ok=True)

@app.route('/api/taches')
@login_required
def api_taches():
    db = get_db(); t = request.args.get('type','')
    sql = "SELECT t.*,z.nom zone,u.prenom||' '||u.nom agent FROM tache t LEFT JOIN zone z ON t.id_zone=z.id LEFT JOIN utilisateur u ON t.id_agent=u.id"
    params = []
    if t: sql += " WHERE t.type=?"; params.append(t)
    sql += " ORDER BY t.created_at DESC"
    rows = db.execute(sql, params).fetchall(); db.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/taches', methods=['POST'])
@login_required
def api_taches_post():
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    d = request.json; db = get_db()
    db.execute("INSERT INTO tache(nom,type,id_zone,id_agent,date_debut_prevue,date_fin_prevue,description) VALUES(?,?,?,?,?,?,?)", (d['nom'],d['type'],d.get('id_zone'),d.get('id_agent'),d.get('date_debut_prevue'),d.get('date_fin_prevue'),d.get('description','')))
    db.commit(); db.close()
    log_action('Nouvelle tache', d['nom']); return jsonify(ok=True)

@app.route('/api/taches/<int:tid>', methods=['PATCH'])
@login_required
def api_tache_patch(tid):
    d = request.json; db = get_db()
    db.execute("UPDATE tache SET statut=? WHERE id=?", (d['statut'],tid))
    db.commit(); db.close()
    log_action('Tache mise a jour', f"ID:{tid} => {d['statut']}"); return jsonify(ok=True)

@app.route('/api/taches/<int:tid>', methods=['DELETE'])
@login_required
def api_tache_del(tid):
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    db = get_db(); db.execute("DELETE FROM tache WHERE id=?", (tid,)); db.commit(); db.close()
    return jsonify(ok=True)

@app.route('/api/compteur/<int:cid>')
@login_required
def api_compteur(cid):
    db = get_db(); r = db.execute("SELECT co.*,cl.nom||' '||cl.prenom client FROM compteur co JOIN client cl ON co.id_client=cl.id WHERE co.id=?", (cid,)).fetchone(); db.close()
    return jsonify(dict(r)) if r else (jsonify(error='Introuvable'), 404)

@app.route('/api/releves')
@login_required
def api_releves():
    db = get_db()
    rows = db.execute("SELECT r.*,co.numero_compteur compteur,cl.nom||' '||cl.prenom client FROM releve r JOIN compteur co ON r.id_compteur=co.id JOIN client cl ON co.id_client=cl.id ORDER BY r.created_at DESC LIMIT 50").fetchall()
    db.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/releves', methods=['POST'])
@login_required
def api_releves_post():
    u = cur_user(); d = request.json; db = get_db()
    c = db.execute("SELECT * FROM compteur WHERE id=?", (d['id_compteur'],)).fetchone()
    if not c: db.close(); return jsonify(error='Compteur introuvable'), 404
    if u['id_zone'] and c['id_zone'] != u['id_zone']:
        db.close(); return jsonify(error="Ce compteur n'appartient pas a votre zone"), 403
    idx = float(d['index_compteur'])
    if idx < float(c['dernier_index']):
        db.close(); return jsonify(error=f"Index invalide — doit etre >= {c['dernier_index']:.3f}"), 400
    vol = round(idx - float(c['dernier_index']), 3)
    db.execute("INSERT INTO releve(id_compteur,date_releve,index_compteur,volume_m3,etat_compteur,commentaires) VALUES(?,?,?,?,?,?)", (d['id_compteur'],d['date_releve'],idx,vol,d.get('etat_compteur','bon'),d.get('commentaires','')))
    db.execute("UPDATE compteur SET dernier_index=?,dernier_relevage=? WHERE id=?", (idx,d['date_releve'],d['id_compteur']))
    db.commit(); db.close()
    log_action('Releve saisi', f"Compteur:{c['numero_compteur']} Vol:{vol:.3f}m3")
    return jsonify(ok=True, volume=vol)

@app.route('/api/releves/<int:rid>', methods=['PATCH'])
@login_required
def api_releve_patch(rid):
    if not is_staff(): return jsonify(error='Acces refuse — staff requis'), 403
    d = request.json; db = get_db()
    db.execute("UPDATE releve SET statut_validation=? WHERE id=?", (d['statut'],rid))
    db.commit(); db.close()
    log_action('Releve valide/rejete', f"ID:{rid} => {d['statut']}"); return jsonify(ok=True)

@app.route('/api/factures')
@login_required
def api_factures():
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    db = get_db(); st = request.args.get('statut',''); mo = request.args.get('mois','')
    sql = "SELECT f.*,c.nom||' '||c.prenom client FROM facture f JOIN client c ON f.id_client=c.id WHERE 1=1"; params = []
    if st: sql += " AND f.statut_paiement=?"; params.append(st)
    if mo: sql += " AND f.date_facture LIKE ?"; params.append(mo+'%')
    sql += " ORDER BY f.date_facture DESC"
    rows = db.execute(sql, params).fetchall(); db.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/factures/stats')
@login_required
def api_factures_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM facture").fetchone()[0]
    payees = db.execute("SELECT COUNT(*) FROM facture WHERE statut_paiement='payee'").fetchone()[0]
    imp = db.execute("SELECT COUNT(*) FROM facture WHERE statut_paiement NOT IN ('payee','annulee')").fetchone()[0]
    enc = db.execute("SELECT COALESCE(SUM(montant_total),0) FROM facture WHERE statut_paiement='payee'").fetchone()[0]
    db.close(); return jsonify(total=total, payees=payees, impayees=imp, total_encaisse=enc)

@app.route('/api/factures', methods=['POST'])
@login_required
def api_factures_post():
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    d = request.json; db = get_db()
    t = db.execute("SELECT * FROM tarification WHERE actif=1 ORDER BY id DESC LIMIT 1").fetchone()
    if not t: db.close(); return jsonify(error='Tarification non configuree'), 500
    t = dict(t); vol = float(d['volume_m3']); cn = vol*t['tarif_m3']; ab = t['frais_abonnement']; base = cn+ab
    tx = base*(t['taxe_tva_pct']+t['taxe_redevance_pct'])/100; cp = t['frais_coupure_montant'] if d.get('frais_coupure') else 0; tot = base+tx+cp
    now = datetime.date.today(); dl = now+datetime.timedelta(days=t['delai_paiement_jours'])
    seq = db.execute("SELECT COUNT(*) FROM facture").fetchone()[0]+1
    num = f"SN-{now.year}-{now.month:02d}-{seq:03d}"
    try:
        db.execute("INSERT INTO facture(id_client,numero_facture,date_facture,periode_debut,periode_fin,date_limite_paiement,montant_consommation,montant_abonnement,montant_taxes,montant_frais_coupure,montant_total,statut_paiement,volume_m3) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (d['id_client'],num,now.isoformat(),d['periode_debut'],d['periode_fin'],dl.isoformat(),cn,ab,tx,cp,tot,'emise',vol))
        db.commit()
    except Exception as e: return jsonify(error=str(e)), 400
    finally: db.close()
    log_action('Facture generee', f"{num} — {int(tot):,} FCFA")
    return jsonify(ok=True, numero=num, montant_total=tot)

@app.route('/api/factures/<int:fid>')
@login_required
def api_facture_detail(fid):
    db = get_db(); f = db.execute("SELECT * FROM facture WHERE id=?", (fid,)).fetchone()
    if not f: db.close(); return jsonify(error='Introuvable'), 404
    c = db.execute("SELECT cl.*,co.numero_compteur compteur FROM client cl LEFT JOIN compteur co ON co.id_client=cl.id WHERE cl.id=?", (f['id_client'],)).fetchone()
    db.close(); r = dict(f); r['client'] = dict(c) if c else {}; return jsonify(r)

@app.route('/api/factures/<int:fid>', methods=['PATCH'])
@login_required
def api_facture_patch(fid):
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    d = request.json; db = get_db()
    db.execute("UPDATE facture SET statut_paiement=? WHERE id=?", (d['statut'],fid))
    if d['statut'] == 'payee':
        db.execute("UPDATE coupure SET statut_coupure='levee' WHERE id_facture=? AND statut_coupure='en_attente'", (fid,))
        f = db.execute("SELECT id_client FROM facture WHERE id=?", (fid,)).fetchone()
        if f: db.execute("UPDATE compteur SET statut_compteur='actif' WHERE id_client=? AND statut_compteur='coupe'", (f['id_client'],))
    db.commit(); db.close()
    log_action('Facture payee', f"ID:{fid}"); return jsonify(ok=True)

@app.route('/api/factures/<int:fid>', methods=['DELETE'])
@login_required
def api_facture_del(fid):
    if not is_admin(): return jsonify(error='Acces refuse — admin requis'), 403
    db = get_db()
    db.execute("DELETE FROM coupure WHERE id_facture=?", (fid,))
    db.execute("DELETE FROM paiement WHERE id_facture=?", (fid,))
    db.execute("DELETE FROM facture WHERE id=?", (fid,))
    db.commit(); db.close()
    log_action('Facture supprimee', f"ID:{fid}"); return jsonify(ok=True)

@app.route('/api/factures/<int:fid>/pdf')
@login_required
def api_facture_pdf(fid):
    if not PDF_OK: return "ReportLab non disponible. Installez : pip install reportlab", 500
    db = get_db(); f = db.execute("SELECT * FROM facture WHERE id=?", (fid,)).fetchone()
    if not f: db.close(); return "Introuvable", 404
    c = db.execute("SELECT cl.*,co.numero_compteur compteur FROM client cl LEFT JOIN compteur co ON co.id_client=cl.id WHERE cl.id=?", (f['id_client'],)).fetchone()
    db.close(); f, c = dict(f), dict(c)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=1.5*cm, bottomMargin=1.5*cm, leftMargin=2*cm, rightMargin=2*cm)
    NV = colors.HexColor('#1B3A6B'); GR = colors.HexColor('#8DC63F')
    def h(txt,size=11,bold=True,color=None,align=TA_LEFT):
        color=color or NV
        return Paragraph(txt, ParagraphStyle('x',fontName='Helvetica-Bold' if bold else 'Helvetica',fontSize=size,textColor=color,alignment=align,leading=size*1.3))
    def p(txt,size=9,color=None,align=TA_LEFT):
        color=color or colors.HexColor('#4A5E74')
        return Paragraph(txt, ParagraphStyle('x',fontName='Helvetica',fontSize=size,textColor=color,alignment=align,leading=size*1.4))
    story = []
    lt = Table([[h("SEN'EAU",22,True,NV), Table([[h("FACTURE",18,True,NV,TA_RIGHT)],[p(f"du {f['date_facture']}",9,colors.HexColor('#8FA3B8'),TA_RIGHT)]],colWidths=[8*cm])]],colWidths=[10*cm,8*cm])
    lt.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LINEBELOW',(0,0),(-1,0),3,GR),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
    story.append(lt); story.append(Spacer(1,0.4*cm))
    at = Table([[p('Grand Dakar — 100 Nelson Diallo Dakar\nTel : 800 11 11',8),Table([[p('Montant a regler',8,colors.HexColor('#102347')),h(f"{int(f['montant_total']):,} FCFA".replace(',',' '),14,True,colors.HexColor('#102347'),TA_RIGHT)],[p(f"Avant le {f['date_limite_paiement']}",8,colors.HexColor('#3a6010'),TA_RIGHT)]],colWidths=[4*cm,5*cm])]],colWidths=[9*cm,9*cm])
    at.setStyle(TableStyle([('BACKGROUND',(1,0),(1,0),GR),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),('LEFTPADDING',(1,0),(1,0),10),('RIGHTPADDING',(1,0),(1,0),10)]))
    story.append(at); story.append(Spacer(1,0.4*cm))
    it = Table([[Table([[p('Informations client',8,colors.HexColor('#8FA3B8'))],[p(f"N Compte : {c.get('numero_compte','—')}",9)],[p(f"N Facture : {f['numero_facture']}",9)],[p(f"N Compteur : {c.get('compteur','—')}",9)],[p(f"Periode : {f['periode_debut']} > {f['periode_fin']}",9)]],colWidths=[8.5*cm]),Table([[p('Destinataire',8,colors.HexColor('#8FA3B8'))],[h(f"{c.get('prenom','')} {c.get('nom','')}",10,True,NV)],[p(c.get('adresse',''),9)],[p(f"Tel : {c.get('telephone','—')}",9)]],colWidths=[8.5*cm])]],colWidths=[9*cm,9*cm])
    it.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#f4f8f2')),('LINEBEFORE',(0,0),(0,0),3,GR),('LINEBEFORE',(1,0),(1,0),3,GR),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),('LEFTPADDING',(0,0),(-1,-1),10)]))
    story.append(it); story.append(Spacer(1,0.4*cm))
    data = [[h('Designation',9,True,colors.HexColor('#102347')),h('Qte',9,True,colors.HexColor('#102347'),TA_RIGHT),h('P.U.',9,True,colors.HexColor('#102347'),TA_RIGHT),h('Montant TTC',9,True,colors.HexColor('#102347'),TA_RIGHT)],
        [p('Consommation eau',9),p(f"{f['volume_m3']} m3",9,align=TA_RIGHT),p(f"{int(f['montant_consommation']/(f['volume_m3'] or 1)):,} FCFA/m3".replace(',',' '),9,align=TA_RIGHT),p(f"{int(f['montant_consommation']):,} FCFA".replace(',',' '),9,NV,TA_RIGHT)],
        [p("Frais d'abonnement",9),p("1 mois",9,align=TA_RIGHT),p(f"{int(f['montant_abonnement']):,} FCFA".replace(',',' '),9,align=TA_RIGHT),p(f"{int(f['montant_abonnement']):,} FCFA".replace(',',' '),9,NV,TA_RIGHT)],
        [p("Taxes (TVA+Redevance)",9),p("—",9,align=TA_RIGHT),p("—",9,align=TA_RIGHT),p(f"{int(f['montant_taxes']):,} FCFA".replace(',',' '),9,NV,TA_RIGHT)]]
    if f['montant_frais_coupure'] > 0:
        data.append([h("Frais de coupure",9,True,colors.HexColor('#c0392b')),p("1",9,align=TA_RIGHT),p(f"{int(f['montant_frais_coupure']):,} FCFA".replace(',',' '),9,align=TA_RIGHT),h(f"{int(f['montant_frais_coupure']):,} FCFA".replace(',',' '),9,True,colors.HexColor('#c0392b'),TA_RIGHT)])
    data.append([h("TOTAL TTC",10,True,NV),p(""),p(""),h(f"{int(f['montant_total']):,} FCFA".replace(',',' '),12,True,NV,TA_RIGHT)])
    dt = Table(data, colWidths=[8*cm,2.5*cm,3.5*cm,4*cm])
    dt.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),GR),('TEXTCOLOR',(0,0),(-1,0),colors.HexColor('#102347')),('ROWBACKGROUNDS',(0,1),(-1,-2),[colors.white,colors.HexColor('#f4f8f2')]),('BACKGROUND',(0,-1),(-1,-1),colors.HexColor('#e8f5d0')),('LINEABOVE',(0,-1),(-1,-1),2,GR),('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#d8e4cc')),('BOTTOMPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),6),('LEFTPADDING',(0,0),(-1,-1),8)]))
    story.append(dt); story.append(Spacer(1,0.5*cm))
    tt = Table([[h(f"{int(f['montant_total']):,} FCFA".replace(',',' '),17,True,colors.HexColor('#102347')),p(f"Statut : {'PAYEE' if f['statut_paiement']=='payee' else 'EN ATTENTE'}\nDate limite : {f['date_limite_paiement']}",9,colors.HexColor('#3a6010'),TA_RIGHT)]],colWidths=[10*cm,8*cm])
    tt.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),GR),('TOPPADDING',(0,0),(-1,-1),12),('BOTTOMPADDING',(0,0),(-1,-1),12),('LEFTPADDING',(0,0),(0,0),16),('RIGHTPADDING',(-1,0),(-1,-1),16)]))
    story.append(tt); story.append(Spacer(1,0.5*cm))
    story.append(HRFlowable(width="100%",thickness=2,color=GR,lineCap='butt',dash=(4,4))); story.append(Spacer(1,0.2*cm))
    talon = Table([[p('TALON A JOINDRE AU PAIEMENT',8,NV),p(f"N : {f['numero_facture']}",8),p(f"Periode : {f['periode_debut']} > {f['periode_fin']}",8),h(f"TOTAL : {int(f['montant_total']):,} FCFA".replace(',',' '),9,True,NV,TA_RIGHT)]],colWidths=[5*cm,5*cm,5*cm,3*cm])
    talon.setStyle(TableStyle([('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]))
    story.append(talon); story.append(Spacer(1,0.2*cm))
    story.append(p("Modes de paiement : Especes · Virement · Orange Money · Wave",7,colors.HexColor('#8FA3B8'),TA_LEFT))
    doc.build(story); buf.seek(0)
    return send_file(buf, download_name=f"Facture_{f['numero_facture']}.pdf", as_attachment=True, mimetype='application/pdf')

@app.route('/api/coupures')
@login_required
def api_coupures():
    db = get_db()
    rows = db.execute("SELECT cu.*,co.numero_compteur compteur,cl.nom||' '||cl.prenom client,f.montant_total montant_facture,f.montant_frais_coupure frais_coupure,f.montant_total total_du FROM coupure cu JOIN compteur co ON cu.id_compteur=co.id JOIN client cl ON co.id_client=cl.id JOIN facture f ON cu.id_facture=f.id WHERE cu.statut_coupure IN ('en_attente','effectuee') ORDER BY cu.id DESC").fetchall()
    db.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/coupures/auto', methods=['POST'])
@login_required
def api_coupures_auto():
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    db = get_db(); t = db.execute("SELECT * FROM tarification WHERE actif=1 ORDER BY id DESC LIMIT 1").fetchone()
    seuil = t['seuil_impayement_jours'] if t else 30; frais = t['frais_coupure_montant'] if t else 5000
    today = datetime.date.today()
    overdue = db.execute("SELECT f.*,co.id cpt_id FROM facture f JOIN client cl ON f.id_client=cl.id JOIN compteur co ON co.id_client=cl.id WHERE f.statut_paiement NOT IN ('payee','annulee') AND date(f.date_limite_paiement) <= date(?) AND f.id NOT IN (SELECT id_facture FROM coupure)", ((today-datetime.timedelta(days=seuil)).isoformat(),)).fetchall()
    created = 0
    for f in overdue:
        db.execute("UPDATE facture SET montant_frais_coupure=?,montant_total=montant_total+?,statut_paiement='depassement_delai' WHERE id=? AND montant_frais_coupure=0", (frais,frais,f['id']))
        db.execute("INSERT INTO coupure(id_compteur,id_facture,date_coupure_prevue) VALUES(?,?,?)", (f['cpt_id'],f['id'],today.isoformat()))
        created += 1
    db.commit(); db.close()
    log_action('Coupures auto', f"{created} generee(s)"); return jsonify(ok=True, created=created)

@app.route('/api/coupures/<int:cid>', methods=['PATCH'])
@login_required
def api_coupure_patch(cid):
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    d = request.json; db = get_db()
    db.execute("UPDATE coupure SET statut_coupure=? WHERE id=?", (d['statut'],cid))
    if d['statut'] == 'effectuee':
        c = db.execute("SELECT id_compteur FROM coupure WHERE id=?", (cid,)).fetchone()
        if c: db.execute("UPDATE compteur SET statut_compteur='coupe' WHERE id=?", (c['id_compteur'],))
    db.commit(); db.close()
    log_action('Coupure effectuee', f"ID:{cid}"); return jsonify(ok=True)

@app.route('/api/coupures/reconnecter', methods=['POST'])
@login_required
def api_reconnecter():
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    d = request.json; db = get_db()
    f = db.execute("SELECT id_client FROM facture WHERE id=?", (d['id_facture'],)).fetchone()
    if f:
        db.execute("UPDATE compteur SET statut_compteur='actif' WHERE id_client=? AND statut_compteur='coupe'", (f['id_client'],))
        db.execute("UPDATE coupure SET statut_coupure='levee' WHERE id_facture=?", (d['id_facture'],))
    db.commit(); db.close()
    log_action('Reconnexion', f"Facture ID:{d['id_facture']}"); return jsonify(ok=True)

@app.route('/api/signalements')
@login_required
def api_signalements():
    db = get_db()
    rows = db.execute("SELECT s.*,z.nom zone FROM signalement s LEFT JOIN zone z ON s.id_zone=z.id ORDER BY s.created_at DESC").fetchall()
    db.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/signalements', methods=['POST'])
@login_required
def api_sign_post():
    u = cur_user(); d = request.json; db = get_db()
    zone = d.get('id_zone') or u['id_zone']
    db.execute("INSERT INTO signalement(type_compromission,id_zone,contenu,urgence) VALUES(?,?,?,?)", (d['type_compromission'],zone,d['contenu'],d.get('urgence','moyen')))
    db.commit(); db.close()
    log_action('Signalement', f"{d['type_compromission']} ({d.get('urgence','moyen')})"); return jsonify(ok=True)

@app.route('/api/signalements/<int:sid>', methods=['PATCH'])
@login_required
def api_sign_patch(sid):
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    db = get_db(); db.execute("UPDATE signalement SET traite=1 WHERE id=?", (sid,)); db.commit(); db.close()
    log_action('Signalement clos', f"ID:{sid}"); return jsonify(ok=True)

@app.route('/api/idees')
@login_required
def api_idees():
    db = get_db(); rows = db.execute("SELECT * FROM idee ORDER BY created_at DESC").fetchall(); db.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/idees', methods=['POST'])
@login_required
def api_idees_post():
    u = cur_user(); d = request.json; db = get_db()
    db.execute("INSERT INTO idee(titre,contenu,categorie,id_auteur) VALUES(?,?,?,?)", (d['titre'],d['contenu'],d.get('categorie',''),u['id']))
    db.commit(); db.close()
    log_action('Idee soumise', d['titre']); return jsonify(ok=True)

@app.route('/api/idees/<int:iid>', methods=['PATCH'])
@login_required
def api_idee_patch(iid):
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    d = request.json; db = get_db()
    db.execute("UPDATE idee SET statut=? WHERE id=?", (d['statut'],iid))
    db.commit(); db.close()
    log_action('Idee mise a jour', f"ID:{iid} => {d['statut']}"); return jsonify(ok=True)

@app.route('/api/tarifs')
@login_required
def api_tarifs():
    db = get_db(); t = db.execute("SELECT * FROM tarification WHERE actif=1 ORDER BY id DESC LIMIT 1").fetchone(); db.close()
    return jsonify(dict(t)) if t else (jsonify(error='Aucun tarif'), 404)

@app.route('/api/tarifs', methods=['POST'])
@login_required
def api_tarifs_post():
    if not is_admin(): return jsonify(error='Acces refuse — admin uniquement'), 403
    d = request.json; today = datetime.date.today().isoformat(); db = get_db()
    db.execute("UPDATE tarification SET actif=0")
    db.execute("INSERT INTO tarification(date_debut,tarif_m3,frais_abonnement,frais_coupure_montant,taxe_tva_pct,taxe_redevance_pct,delai_paiement_jours,seuil_impayement_jours,actif) VALUES(?,?,?,?,?,?,?,?,1)", (today,d['tarif_m3'],d['frais_abonnement'],d['frais_coupure_montant'],d['taxe_tva_pct'],d['taxe_redevance_pct'],d['delai_paiement_jours'],d['seuil_impayement_jours']))
    db.commit(); t = db.execute("SELECT * FROM tarification WHERE actif=1 ORDER BY id DESC LIMIT 1").fetchone(); db.close()
    log_action('Tarif mis a jour', f"m3:{d['tarif_m3']} FCFA"); return jsonify(dict(t))

@app.route('/api/journal')
@login_required
def api_journal():
    if not is_staff(): return jsonify(error='Acces refuse'), 403
    db = get_db()
    rows = db.execute("SELECT j.*,u.nom,u.prenom,u.role FROM journal j LEFT JOIN utilisateur u ON j.id_utilisateur=u.id ORDER BY j.created_at DESC LIMIT 200").fetchall()
    db.close(); return jsonify([dict(r) for r in rows])

@app.route('/api/notifications')
@login_required
def api_notifications():
    db = get_db()
    fi = db.execute("SELECT COUNT(*) FROM facture WHERE statut_paiement NOT IN ('payee','annulee')").fetchone()[0]
    cc = db.execute("SELECT COUNT(*) FROM coupure WHERE statut_coupure='en_attente'").fetchone()[0]
    rv = db.execute("SELECT COUNT(*) FROM releve WHERE statut_validation='en_attente'").fetchone()[0]
    db.close()
    notifs = []
    if fi > 0: notifs.append({"titre":f"{fi} factures impayes","detail":"Pensez a generer les coupures automatiques","bg":"background:#fff8e6"})
    if cc > 0: notifs.append({"titre":f"{cc} coupure(s) a effectuer","detail":"Des agents doivent etre affectes","bg":"background:#fdecea"})
    if rv > 0: notifs.append({"titre":f"{rv} releve(s) en attente","detail":"Verifiez les anomalies avant de valider","bg":"background:#e8f5d0"})
    notifs.append({"titre":"Systeme operationnel","detail":"Base de donnees SQLite connectee et fonctionnelle","bg":"background:#e8f5d0"})
    return jsonify(notifs)

# ═══════════════════════════════════════════════════════════
if __name__ == '__main__':
    print("="*60)
    print("  SEN'EAU — Plateforme de Gestion")
    init_db()
    print(f"  Base de donnees : {os.path.abspath(DB)}")
    print(f"  PDF ReportLab  : {'OK' if PDF_OK else 'NON DISPONIBLE (pip install reportlab)'}")
    print("="*60)
    app.run(debug=False, host='0.0.0.0', port=5000)
