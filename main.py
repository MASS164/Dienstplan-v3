import io
import re
import os
from datetime import datetime, time, timedelta
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

# Optional für WhatsApp-Versand via Twilio (pip install twilio)
try:
    from twilio.rest import Client as TwilioClient
except ImportError:
    TwilioClient = None

app = FastAPI(title="Sicherheits-Dienstplan Enterprise Suite Pro + Zeiterfassung & Urlaub")

# ==========================================
# STAMMDATEN & KONFIGURATION
# ==========================================

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "DEINE_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "DEIN_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
CHEF_WHATSAPP_NUMBER = os.getenv("CHEF_WHATSAPP_NUMBER", "whatsapp:+491700000000")

MITARBEITER_KUERZEL = {
    "TH": "Tizian",
    "TG": "Tom G",
    "NP": "Nico P",
    "RH": "Roland H",
    "JL": "Jeason L",
}

OBJEKT_NAMEN = {
    "UM": "Maltry",
    "GH": "Golfhotel"
}

FEIERTAGE = [
    "01.01.2026", "03.04.2026", "06.04.2026", "01.05.2026", "14.05.2026",
    "25.05.2026", "03.10.2026", "25.12.2026", "26.12.2026",
    "01.01.2027", "26.03.2027", "29.03.2027", "01.05.2027", "06.05.2027"
]

USERS = {
    "chef": {"password": "chef123", "role": "chef", "name": "Chef / Admin", "handy": CHEF_WHATSAPP_NUMBER},
    "Tizian": {"password": "1234", "role": "mitarbeiter", "name": "Tizian", "kuerzel": "TH", "handy": "whatsapp:+491701111111"},
    "Tom G": {"password": "1234", "role": "mitarbeiter", "name": "Tom G", "kuerzel": "TG", "handy": "whatsapp:+491702222222"},
    "Nico P": {"password": "1234", "role": "mitarbeiter", "name": "Nico P", "kuerzel": "NP", "handy": "whatsapp:+491703333333"},
    "Roland H": {"password": "1234", "role": "mitarbeiter", "name": "Roland H", "kuerzel": "RH", "handy": "whatsapp:+491704444444"},
    "Jeason L": {"password": "1234", "role": "mitarbeiter", "name": "Jeason L", "kuerzel": "JL", "handy": "whatsapp:+491705555555"},
}

MUSTER_SCHICHTEN = [
    {"tag_offset": 0, "objekt": "Maltry", "zeit": "06:00 - 14:00", "stunden": "8", "mitarbeiter": "Tizian"},
    {"tag_offset": 0, "objekt": "Maltry", "zeit": "14:00 - 22:00", "stunden": "8", "mitarbeiter": "Tom G"},
    {"tag_offset": 0, "objekt": "Maltry", "zeit": "22:00 - 06:00", "stunden": "8", "mitarbeiter": "Nico P"},
    {"tag_offset": 1, "objekt": "Golfhotel", "zeit": "22:00 - 06:00", "stunden": "8", "mitarbeiter": "Roland H"},
    {"tag_offset": 2, "objekt": "Maltry", "zeit": "22:00 - 06:00", "stunden": "8", "mitarbeiter": "Jeason L"},
]

# GLOBALER SPEICHER
aktuelle_schichten = []
warnungen = []
chef_einstellungen = {
    "max_wochenstunden": 48,
    "verspaetung_toleranz_min": 15
}

# ZEITERFASSUNG & STEMPEL-LOG
time_tracking_data = {}
stempel_historie = []

# URLAUBSVERWALTUNG
urlaubs_antraege = [
    {"id": 1, "mitarbeiter": "Tizian", "von": "15.08.2026", "bis": "22.08.2026", "grund": "Erholungsurlaub", "status": "Offen", "erstellt_am": "03.08.2026 09:15"},
    {"id": 2, "mitarbeiter": "Tom G", "von": "01.09.2026", "bis": "05.09.2026", "grund": "Familienfeier", "status": "Offen", "erstellt_am": "03.08.2026 11:30"}
]
antrag_id_counter = 3


# ==========================================
# HELFER-FUNKTIONEN (WHATSAPP & BERECHNUNG)
# ==========================================

def send_whatsapp(empfaenger_nummer: str, nachricht_text: str):
    """Verschickt eine WhatsApp-Nachricht via Twilio API"""
    if not empfaenger_nummer:
        return False
    if not empfaenger_nummer.startswith("whatsapp:"):
        empfaenger_nummer = f"whatsapp:{empfaenger_nummer}"

    if TwilioClient and TWILIO_ACCOUNT_SID != "DEINE_ACCOUNT_SID":
        try:
            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            message = client.messages.create(
                body=nachricht_text,
                from_=TWILIO_WHATSAPP_NUMBER,
                to=empfaenger_nummer
            )
            print(f"[WhatsApp OK] Nachricht an {empfaenger_nummer} gesendet (SID: {message.sid})")
            return True
        except Exception as e:
            print(f"[WhatsApp Fehler] {e}")
            return False
    else:
        print(f"[WhatsApp Simulation] An {empfaenger_nummer}:\n{nachricht_text}\n")
        return True

def parse_schicht_zeit(zeit_str: str):
    try:
        times = re.findall(r'(\d{1,2}:\d{2})', str(zeit_str))
        if len(times) >= 2:
            t1 = datetime.strptime(times[0], "%H:%M").time()
            t2 = datetime.strptime(times[1], "%H:%M").time()
            return t1, t2
    except Exception:
        pass
    return None, None

def berechne_nachtstunden(t1: time, t2: time, total_std: float) -> float:
    if not t1 or not t2:
        return 0.0
    start_min = t1.hour * 60 + t1.minute
    end_min = t2.hour * 60 + t2.minute
    if end_min <= start_min:
        end_min += 24 * 60
    nacht_start = 22 * 60 # 22:00 Uhr
    nacht_end = 30 * 60   # 06:00 Uhr
    overlap_start = max(start_min, nacht_start)
    overlap_end = min(end_min, nacht_end)
    if overlap_end > overlap_start:
        return round((overlap_end - overlap_start) / 60.0, 2)
    if start_min < 6 * 60:
        f_end = min(end_min, 6 * 60)
        return round((f_end - start_min) / 60.0, 2)
    return 0.0

def berechne_mitarbeiter_stunden_statistik():
    statistik = {u: {"gesamt": 0.0, "tag": 0.0, "nacht": 0.0, "sonntag": 0.0, "feiertag": 0.0, "schichten_anzahl": 0} 
                 for u, info in USERS.items() if info["role"] == "mitarbeiter"}

    for s in aktuelle_schichten:
        datum_str = s.get("datum", "")
        zeit_str = s.get("zeit", "")
        try:
            stunden = float(str(s.get("stunden", "0")).replace(",", "."))
        except ValueError:
            stunden = 8.0

        is_sonntag = False
        is_feiertag = datum_str in FEIERTAGE
        try:
            if datetime.strptime(datum_str, "%d.%m.%Y").weekday() == 6:
                is_sonntag = True
        except ValueError:
            pass

        t1, t2 = parse_schicht_zeit(zeit_str)
        nacht_std = berechne_nachtstunden(t1, t2, stunden) if (t1 and t2) else 0.0
        tag_std = max(0.0, stunden - nacht_std)

        for m in [x.strip() for x in s.get("mitarbeiter", "").split(",") if x.strip()]:
            if m in statistik:
                statistik[m]["gesamt"] += stunden
                statistik[m]["tag"] += tag_std
                statistik[m]["nacht"] += nacht_std
                if is_sonntag: statistik[m]["sonntag"] += stunden
                if is_feiertag: statistik[m]["feiertag"] += stunden
                statistik[m]["schichten_anzahl"] += 1
    return statistik


# ==========================================
# AUTHENTIFIZIERUNG & BASIC ROUTEN
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user_role = request.cookies.get("session_role")
    if user_role == "chef":
        return RedirectResponse(url="/chef-dashboard", status_code=303)
    elif user_role == "mitarbeiter":
        return RedirectResponse(url="/mitarbeiter-dashboard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
async def login_page(msg: str = ""):
    ma_options = "".join([f"<option value='{name}'>{name}</option>" for name in USERS.keys() if USERS[name]["role"] == "mitarbeiter"])
    msg_html = f"<p style='color: #f87171; text-align:center;'>{msg}</p>" if msg else ""

    html = f"""
    <!DOCTYPE html>
    <html lang="de">
    <head>
        <meta charset="UTF-8">
        <title>Login - Sicherheits Dienstplan</title>
        <style>
            body {{ font-family: sans-serif; background-color: #0f172a; color: white; display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }}
            .login-box {{ background: #1e293b; padding: 30px; border-radius: 12px; border: 1px solid #334155; width: 360px; }}
            h2 {{ color: #38bdf8; margin-top:0; text-align:center; }}
            input, select, button {{ width: 100%; padding: 10px; margin-top: 10px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: white; box-sizing: border-box; }}
            button {{ background: #0284c7; font-weight: bold; cursor: pointer; border:none; margin-top:15px; }}
            .tab-btn {{ background: #334155; width: 48%; display: inline-block; text-align: center; cursor: pointer; padding: 8px 0; border-radius: 6px; font-weight: bold; font-size: 13px; }}
            .tab-active {{ background: #0284c7; }}
        </style>
    </head>
    <body>
        <div class="login-box">
            <h2>🛡️ Dienstplan Login</h2>
            {msg_html}
            <div style="display:flex; justify-content:space-between; margin-bottom: 15px;">
                <div id="tab1" class="tab-btn tab-active" onclick="showTab('ma')">👷 Mitarbeiter</div>
                <div id="tab2" class="tab-btn" onclick="showTab('chef')">👨‍🍳 Chef / Admin</div>
            </div>
            <form id="ma_form" action="/do-login" method="post">
                <input type="hidden" name="login_type" value="mitarbeiter">
                <select name="username">{ma_options}</select>
                <input type="password" name="password" required placeholder="Passwort">
                <button type="submit">Als Mitarbeiter einloggen</button>
            </form>
            <form id="chef_form" action="/do-login" method="post" style="display:none;">
                <input type="hidden" name="login_type" value="chef">
                <input type="text" name="username" value="chef" required>
                <input type="password" name="password" required placeholder="Passwort">
                <button type="submit" style="background:#6d28d9;">Als Chef einloggen</button>
            </form>
        </div>
        <script>
            function showTab(type) {{
                document.getElementById('ma_form').style.display = type === 'ma' ? 'block' : 'none';
                document.getElementById('chef_form').style.display = type === 'chef' ? 'block' : 'none';
                document.getElementById('tab1').className = 'tab-btn ' + (type === 'ma' ? 'tab-active' : '');
                document.getElementById('tab2').className = 'tab-btn ' + (type === 'chef' ? 'tab-active' : '');
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.post("/do-login")
async def do_login(username: str = Form(...), password: str = Form(...)):
    user_info = USERS.get(username)
    if not user_info or user_info["password"] != password:
        return RedirectResponse(url="/login?msg=Ungueltige+Anmeldedaten", status_code=303)
    redirect_target = "/chef-dashboard" if user_info["role"] == "chef" else "/mitarbeiter-dashboard"
    resp = RedirectResponse(url=redirect_target, status_code=303)
    resp.set_cookie(key="session_user", value=username)
    resp.set_cookie(key="session_role", value=user_info["role"])
    return resp

@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("session_user")
    resp.delete_cookie("session_role")
    return resp


# ==========================================
# URLAUBSVERWALTUNG (ROUTEN)
# ==========================================

@app.post("/urlaub-beantragen")
async def urlaub_beantragen(
    request: Request,
    background_tasks: BackgroundTasks,
    von: str = Form(...),
    bis: str = Form(...),
    grund: str = Form(...)
):
    global antrag_id_counter
    user = request.cookies.get("session_user")
    if not user or USERS.get(user, {}).get("role") != "mitarbeiter":
        return RedirectResponse(url="/login", status_code=303)

    try:
        if "-" in von:
            von = datetime.strptime(von, "%Y-%m-%d").strftime("%d.%m.%Y")
        if "-" in bis:
            bis = datetime.strptime(bis, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        pass

    erstellt_am = datetime.now().strftime("%d.%m.%Y %H:%M")
    
    neuer_antrag = {
        "id": antrag_id_counter,
        "mitarbeiter": user,
        "von": von,
        "bis": bis,
        "grund": grund,
        "status": "Offen",
        "erstellt_am": erstellt_am
    }
    urlaubs_antraege.append(neuer_antrag)
    antrag_id_counter += 1

    text = f"🌴 *NEUER URLAUBSANTRAG*\n\n" \
           f"👤 Mitarbeiter: *{user}*\n" \
           f"📅 Zeitraum: {von} bis {bis}\n" \
           f"📝 Grund: {grund}\n\n" \
           f"Bitte im Dashboard prüfen & genehmigen/ablehnen."
    background_tasks.add_task(send_whatsapp, CHEF_WHATSAPP_NUMBER, text)

    return RedirectResponse(url="/mitarbeiter-dashboard?msg=Antrag+erfolgreich+eingereicht", status_code=303)

@app.post("/urlaub-genehmigen/{antrag_id}")
async def urlaub_genehmigen(antrag_id: int, background_tasks: BackgroundTasks, request: Request):
    if request.cookies.get("session_role") != "chef":
        return RedirectResponse(url="/login", status_code=303)

    antrag = next((a for a in urlaubs_antraege if a["id"] == antrag_id), None)
    if antrag:
        antrag["status"] = "Genehmigt"
        ma_info = USERS.get(antrag["mitarbeiter"], {})
        handy = ma_info.get("handy")
        if handy:
            text = f"✅ *URLAUBSANTRAG GENEHMIGT*\n\n" \
                   f"Hallo {antrag['mitarbeiter']},\n" \
                   f"dein Urlaubsantrag vom *{antrag['von']} bis {antrag['bis']}* wurde soeben *GENEHMIGT*! 🎉"
            background_tasks.add_task(send_whatsapp, handy, text)

    return RedirectResponse(url="/chef-dashboard", status_code=303)

@app.post("/urlaub-ablehnen/{antrag_id}")
async def urlaub_ablehnen(antrag_id: int, background_tasks: BackgroundTasks, request: Request):
    if request.cookies.get("session_role") != "chef":
        return RedirectResponse(url="/login", status_code=303)

    antrag = next((a for a in urlaubs_antraege if a["id"] == antrag_id), None)
    if antrag:
        antrag["status"] = "Abgelehnt"
        ma_info = USERS.get(antrag["mitarbeiter"], {})
        handy = ma_info.get("handy")
        if handy:
            text = f"❌ *URLAUBSANTRAG ABGELEHNT*\n\n" \
                   f"Hallo {antrag['mitarbeiter']},\n" \
                   f"dein Urlaubsantrag vom *{antrag['von']} bis {antrag['bis']}* wurde leider *ABGELEHNT*. Bitte sprich mit der Einsatzleitung."
            background_tasks.add_task(send_whatsapp, handy, text)

    return RedirectResponse(url="/chef-dashboard", status_code=303)


# ==========================================
# MUSTER-DIENSTPLAN & WHATSAPP FEATURES
# ==========================================

@app.post("/muster-dienstplan-uebernehmen")
async def muster_dienstplan_uebernehmen(start_datum: str = Form(...)):
    global aktuelle_schichten
    try:
        base_date = datetime.strptime(start_datum, "%Y-%m-%d")
    except ValueError:
        return RedirectResponse(url="/chef-dashboard?msg=Ungueltiges+Datum", status_code=303)

    neue_schichten = []
    for m in MUSTER_SCHICHTEN:
        schicht_datum = (base_date + timedelta(days=m["tag_offset"])).strftime("%d.%m.%Y")
        neue_schichten.append({
            "datum": schicht_datum,
            "objekt": m["objekt"],
            "zeit": m["zeit"],
            "stunden": m["stunden"],
            "mitarbeiter": m["mitarbeiter"]
        })

    aktuelle_schichten.extend(neue_schichten)
    return RedirectResponse(url="/chef-dashboard", status_code=303)

@app.post("/check-puenktlichkeit-und-alarm")
async def check_puenktlichkeit_und_alarm(background_tasks: BackgroundTasks):
    jetzt = datetime.now()
    heute_str = jetzt.strftime("%d.%m.%Y")
    toleranz = chef_einstellungen["verspaetung_toleranz_min"]
    
    verspaetete_mitarbeiter = []

    for s in aktuelle_schichten:
        if s.get("datum") == heute_str:
            t1, _ = parse_schicht_zeit(s.get("zeit", ""))
            if t1:
                schicht_start = datetime.combine(jetzt.date(), t1)
                toleranz_zeit = schicht_start + timedelta(minutes=toleranz)
                
                if jetzt > toleranz_zeit:
                    ma_namen = [x.strip() for x in s.get("mitarbeiter", "").split(",")]
                    for ma in ma_namen:
                        status = time_tracking_data.get(ma, {}).get("status", "Abgemeldet")
                        if status != "Anwesend":
                            verspaetete_mitarbeiter.append({
                                "name": ma,
                                "objekt": s.get("objekt"),
                                "zeit": s.get("zeit"),
                                "verspaetung_min": int((jetzt - schicht_start).total_seconds() / 60)
                            })

    if verspaetete_mitarbeiter:
        text = "🚨 *ALARM: UNPÜNKTLICHKEIT / FEHLENDER DIENSTBEGINN!*\n\n"
        for v in verspaetete_mitarbeiter:
            text += f"⚠️ *{v['name']}* hat sich noch NICHT angemeldet!\n"
            text += f"📍 Objekt: {v['objekt']}\n"
            text += f"⏰ Geplant: {v['zeit']} (vor {v['verspaetung_min']} Min)\n\n"
        
        text += "Bitte umgehende Prüfung im Dashboard!"
        background_tasks.add_task(send_whatsapp, CHEF_WHATSAPP_NUMBER, text)

    return RedirectResponse(url="/chef-dashboard", status_code=303)

@app.post("/whatsapp-dienstplan-versenden")
async def whatsapp_dienstplan_versenden(background_tasks: BackgroundTasks):
    if not aktuelle_schichten:
        return RedirectResponse(url="/chef-dashboard", status_code=303)

    plan_text = "📅 *AKTUELLER SCHICHTPLAN / DIENSTPLAN*\n\n"
    for s in aktuelle_schichten:
        plan_text += f"🔹 *{s['datum']}* | {s['objekt']}\n"
        plan_text += f"   ⏰ Zeit: {s['zeit']} ({s['stunden']} Std)\n"
        plan_text += f"   👷 Eingeteilt: {s['mitarbeiter']}\n\n"

    plan_text += "📄 *Vollständige PDF-Übersicht zum Download/Druck:*\n"
    plan_text += "http://localhost:10000/stundenuebersicht-drucken"

    for username, info in USERS.items():
        handy = info.get("handy")
        if handy:
            background_tasks.add_task(send_whatsapp, handy, plan_text)

    return RedirectResponse(url="/chef-dashboard", status_code=303)


# ==========================================
# STUNDENÜBERSICHT PDF / DRUCK-VIEW
# ==========================================

@app.get("/stundenuebersicht-drucken", response_class=HTMLResponse)
async def stundenuebersicht_drucken(request: Request):
    statistik = berechne_mitarbeiter_stunden_statistik()
    rows_html = ""
    total_g = total_t = total_n = total_s = total_f = 0
    
    for emp, daten in statistik.items():
        total_g += daten["gesamt"]
        total_t += daten["tag"]
        total_n += daten["nacht"]
        total_s += daten["sonntag"]
        total_f += daten["feiertag"]
        rows_html += f"""
        <tr>
            <td><b>{emp}</b></td>
            <td>{daten['schichten_anzahl']}</td>
            <td><b>{daten['gesamt']:.2f} Std</b></td>
            <td>{daten['tag']:.2f} Std</td>
            <td><span style="color:#6366f1; font-weight:bold;">{daten['nacht']:.2f} Std</span></td>
            <td><span style="color:#059669; font-weight:bold;">{daten['sonntag']:.2f} Std</span></td>
            <td><span style="color:#d97706; font-weight:bold;">{daten['feiertag']:.2f} Std</span></td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="de">
    <head>
        <meta charset="UTF-8">
        <title>Monatsabrechnung - Stundenübersicht</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 30px; background: white; color:#0f172a; }}
            .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #0f172a; padding-bottom: 10px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            th, td {{ padding: 10px; border-bottom: 1px solid #cbd5e1; font-size: 12px; text-align: left; }}
            th {{ background: #f1f5f9; }}
            .tfoot {{ font-weight: bold; background: #e2e8f0; border-top: 2px solid #0f172a; }}
            @media print {{ .no-print {{ display: none; }} }}
        </style>
    </head>
    <body>
        <div class="no-print" style="margin-bottom:20px;">
            <button onclick="window.print()" style="background:#0284c7; color:white; border:none; padding:10px 15px; border-radius:5px; cursor:pointer;">🖨️ Als PDF Speichern / Drucken</button>
            <a href="/chef-dashboard" style="margin-left:10px; text-decoration:none; color:#64748b;">Zurück</a>
        </div>
        <div class="header">
            <h2>🛡️ Sicherheits-Dienstplan: Stunden & Zuschläge</h2>
            <div>Datum: {datetime.now().strftime("%d.%m.%Y")}</div>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Mitarbeiter</th><th>Schichten</th><th>Gesamt Std</th><th>Tag Std</th>
                    <th>Nacht (22-06h)</th><th>Sonntags-Std</th><th>Feiertags-Std</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
                <tr class="tfoot">
                    <td>SUMME GESAMT</td><td>-</td><td>{total_g:.2f} Std</td><td>{total_t:.2f} Std</td>
                    <td>{total_n:.2f} Std</td><td>{total_s:.2f} Std</td><td>{total_f:.2f} Std</td>
                </tr>
            </tbody>
        </table>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


# ==========================================
# CHEF DASHBOARD
# ==========================================

@app.get("/chef-dashboard", response_class=HTMLResponse)
async def chef_dashboard(request: Request):
    if request.cookies.get("session_role") != "chef":
        return RedirectResponse(url="/login", status_code=303)

    user = request.cookies.get("session_user", "Chef")

    # Mitarbeiterliste
    ma_liste_html = ""
    ma_options_html = ""
    for u_name, u_info in USERS.items():
        if u_info["role"] == "mitarbeiter":
            kuerzel = u_info.get("kuerzel", "-")
            ma_options_html += f"<option value='{u_name}'>{u_name} ({kuerzel})</option>"
            ma_liste_html += f"""
            <tr>
                <td><b>{u_name}</b></td>
                <td><span class='badge-user'>{kuerzel}</span></td>
                <td><small>{u_info.get('handy','-')}</small></td>
                <td>
                    <form action="/mitarbeiter-loeschen/{u_name}" method="post" style="margin:0;">
                        <button type="submit" class="btn-sm btn-danger">X</button>
                    </form>
                </td>
            </tr>
            """

    # Aktuelle Schichten Tabelle
    tabelle_html = ""
    for idx, s in enumerate(aktuelle_schichten):
        tabelle_html += f"""
        <tr>
            <td>{s.get('datum','-')}</td>
            <td><b>{s.get('objekt','-')}</b></td>
            <td>{s.get('zeit','-')}</td>
            <td>{s.get('stunden','-')} Std</td>
            <td><span class='badge-user'>{s.get('mitarbeiter','-')}</span></td>
            <td>
                <form action="/schicht-loeschen/{idx}" method="post" style="margin:0;">
                    <button type="submit" class="btn-sm btn-danger">X</button>
                </form>
            </td>
        </tr>
        """

    # Stundenstatistik
    statistik = berechne_mitarbeiter_stunden_statistik()
    stunden_uebersicht_html = ""
    for emp, daten in statistik.items():
        stunden_uebersicht_html += f"""
        <tr>
            <td><b>{emp}</b></td>
            <td><b>{daten['gesamt']:.2f} Std</b></td>
            <td>{daten['tag']:.2f} Std</td>
            <td><span style="color:#818cf8;">{daten['nacht']:.2f} Std</span></td>
            <td><span style="color:#4ade80;">{daten['sonntag']:.2f} Std</span></td>
            <td><span style="color:#fbbf24;">{daten['feiertag']:.2f} Std</span></td>
        </tr>
        """

    # Live Stempeluhr-Übersicht für Chef
    stempel_html = ""
    for emp, data in time_tracking_data.items():
        status_text = "● Anwesend" if data.get("status") == "Anwesend" else "● Abgemeldet"
        status_class = "status-an" if data.get("status") == "Anwesend" else "status-aus"
        ein_zeit = data.get("einstempel_zeit", "-")
        aus_zeit = data.get("ausstempel_zeit", "-")
        dauer = data.get("letzte_dauer", "-")

        stempel_html += f"""
        <tr>
            <td><b>{emp}</b></td>
            <td><span class='{status_class}'>{status_text}</span></td>
            <td><span style="color:#4ade80; font-weight:bold;">{ein_zeit}</span></td>
            <td><span style="color:#f87171; font-weight:bold;">{aus_zeit}</span></td>
            <td><b>{dauer}</b></td>
        </tr>
        """

    # Chronologisches Stempel-Protokoll (Historie)
    log_html = ""
    for entry in stempel_historie[:15]:
        log_html += f"""
        <tr>
            <td><small>{entry['zeit']}</small></td>
            <td><b>{entry['mitarbeiter']}</b></td>
            <td>{entry['aktion']}</td>
            <td>{entry['dauer']}</td>
        </tr>
        """

    # Urlaubsanträge Tabellen
    offene_antraege_html = ""
    bearbeitete_antraege_html = ""

    for a in urlaubs_antraege:
        if a["status"] == "Offen":
            offene_antraege_html += f"""
            <tr>
                <td><b>{a['mitarbeiter']}</b></td>
                <td>{a['von']} - {a['bis']}</td>
                <td>{a['grund']}</td>
                <td><small style="color:#94a3b8;">{a['erstellt_am']}</small></td>
                <td style="display:flex; gap:5px;">
                    <form action="/urlaub-genehmigen/{a['id']}" method="post" style="margin:0;">
                        <button type="submit" class="btn-sm" style="background:#16a34a;">✓ Genehmigen</button>
                    </form>
                    <form action="/urlaub-ablehnen/{a['id']}" method="post" style="margin:0;">
                        <button type="submit" class="btn-sm btn-danger">✗ Ablehnen</button>
                    </form>
                </td>
            </tr>
            """
        else:
            status_color = "#4ade80" if a["status"] == "Genehmigt" else "#f87171"
            bearbeitete_antraege_html += f"""
            <tr>
                <td><b>{a['mitarbeiter']}</b></td>
                <td>{a['von']} - {a['bis']}</td>
                <td>{a['grund']}</td>
                <td><span style="color:{status_color}; font-weight:bold;">{a['status']}</span></td>
            </tr>
            """

    if not offene_antraege_html:
        offene_antraege_html = "<tr><td colspan='5' style='text-align:center; color:#94a3b8;'>Keine offenen Urlaubsanträge vorhanden.</td></tr>"
    if not bearbeitete_antraege_html:
        bearbeitete_antraege_html = "<tr><td colspan='4' style='text-align:center; color:#94a3b8;'>Bisher keine Historie.</td></tr>"

    html = f"""
    <!DOCTYPE html>
    <html lang="de">
    <head>
        <meta charset="UTF-8">
        <title>Chef Dashboard - Enterprise Pro</title>
        <style>
            body {{ font-family: sans-serif; background-color: #0f172a; color: white; padding: 20px; margin:0; }}
            .container {{ max-width: 1300px; margin: auto; }}
            header {{ display: flex; justify-content: space-between; align-items: center; background: #1e293b; padding: 15px 25px; border-radius: 12px; border: 1px solid #334155; margin-bottom: 20px; }}
            .grid-3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
            .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
            .card {{ background: #1e293b; padding: 20px; border-radius: 12px; border: 1px solid #334155; margin-bottom: 20px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ padding: 8px 10px; border-bottom: 1px solid #334155; text-align: left; font-size:13px; }}
            th {{ background: #0f172a; color: #94a3b8; }}
            button, .btn {{ background: #0284c7; color: white; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: bold; text-decoration: none; display:inline-block; }}
            .btn-sm {{ padding: 5px 10px; font-size: 11px; }}
            .btn-danger {{ background: #e11d48; }}
            .btn-whatsapp {{ background: #25D366; color: black; font-weight: bold; }}
            .badge-user {{ background: #0369a1; padding: 2px 6px; border-radius: 4px; font-size: 11px; }}
            .status-an {{ color: #4ade80; font-weight: bold; }}
            .status-aus {{ color: #f87171; font-weight: bold; }}
            input, select {{ background: #0f172a; border: 1px solid #334155; color: white; padding: 8px; border-radius: 6px; width: 100%; box-sizing: border-box; margin-bottom: 10px; font-size: 13px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h2 style="margin:0; color:#38bdf8;">👨‍🍳 Chef-Zentrale | {user}</h2>
                <a href="/logout" class="btn btn-danger">Abmelden</a>
            </header>

            <!-- SCHNELL-AKTIONEN -->
            <div class="card" style="border: 1px solid #25D366;">
                <h3>📲 WhatsApp-Zentrale & Pünktlichkeits-Alarm</h3>
                <div style="display:flex; gap:15px; flex-wrap:wrap;">
                    <form action="/whatsapp-dienstplan-versenden" method="post">
                        <button type="submit" class="btn btn-whatsapp">📱 Dienstplan (PDF-Link) per WhatsApp an ALLE senden</button>
                    </form>
                    <form action="/check-puenktlichkeit-und-alarm" method="post">
                        <button type="submit" style="background:#e11d48;">🚨 Unpünktlichkeit prüfen & Alarm auslösen</button>
                    </form>
                </div>
            </div>

            <!-- LIVE ZEITERFASSUNG DER MITARBEITER -->
            <div class="card" style="border: 1px solid #38bdf8;">
                <h3>⏱️ Live-Zeiterfassung der Mitarbeiter</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Mitarbeiter</th>
                            <th>Status</th>
                            <th>Eingestempelt um</th>
                            <th>Ausgestempelt um</th>
                            <th>Geleistete Schichtdauer</th>
                        </tr>
                    </thead>
                    <tbody>
                        {stempel_html if stempel_html else '<tr><td colspan="5" style="text-align:center; color:#94a3b8;">Keine aktiven Stempeldaten vorhanden.</td></tr>'}
                    </tbody>
                </table>
            </div>

            <!-- STEMPEL HISTORIE LOG -->
            <div class="card">
                <h3>📜 Chronologisches Stempel-Protokoll</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Zeitpunkt</th>
                            <th>Mitarbeiter</th>
                            <th>Aktion</th>
                            <th>Errechnete Dauer</th>
                        </tr>
                    </thead>
                    <tbody>
                        {log_html if log_html else '<tr><td colspan="4" style="text-align:center; color:#94a3b8;">Bisher keine Stempelvorgänge protokolliert.</td></tr>'}
                    </tbody>
                </table>
            </div>

            <!-- URLAUBSVERWALTUNG -->
            <div class="grid-2">
                <div class="card" style="border: 1px solid #eab308;">
                    <h3 style="color:#facc15;">🌴 Offene Urlaubsanträge</h3>
                    <table>
                        <thead>
                            <tr><th>Mitarbeiter</th><th>Zeitraum</th><th>Grund</th><th>Beantragt am</th><th>Aktion</th></tr>
                        </thead>
                        <tbody>{offene_antraege_html}</tbody>
                    </table>
                </div>

                <div class="card">
                    <h3>📜 Urlaubsanträge Historie</h3>
                    <table>
                        <thead>
                            <tr><th>Mitarbeiter</th><th>Zeitraum</th><th>Grund</th><th>Status</th></tr>
                        </thead>
                        <tbody>{bearbeitete_antraege_html}</tbody>
                    </table>
                </div>
            </div>

            <div class="grid-3">
                <div class="card">
                    <h3>📋 Muster-Dienstplan übernehmen</h3>
                    <form action="/muster-dienstplan-uebernehmen" method="post">
                        <label style="font-size:11px; color:#94a3b8;">Startdatum (Montag):</label>
                        <input type="date" name="start_datum" required>
                        <button type="submit" style="background:#6d28d9;">Standardmuster einpflegen</button>
                    </form>
                </div>

                <div class="card">
                    <h3>📁 Excel Upload</h3>
                    <form action="/upload-excel-dienstplan" method="post" enctype="multipart/form-data">
                        <input type="file" name="file" accept=".xlsx, .xls" required>
                        <button type="submit">Einlesen & Prüfen</button>
                    </form>
                </div>

                <div class="card">
                    <h3>⚙️ Einstellungen</h3>
                    <form action="/chef-einstellungen" method="post">
                        <label style="font-size:11px; color:#94a3b8;">Max Wochenstunden:</label>
                        <input type="number" name="max_wochenstunden" value="{chef_einstellungen['max_wochenstunden']}">
                        <button type="submit">Speichern</button>
                    </form>
                </div>
            </div>

            <!-- STUNDENÜBERSICHT CARD -->
            <div class="card">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 10px;">
                    <h3 style="margin:0;">📊 Monatliche Stundenübersicht & Zuschläge</h3>
                    <a href="/stundenuebersicht-drucken" target="_blank" class="btn">🖨️ Druckansicht / PDF</a>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Mitarbeiter</th><th>Gesamt</th><th>Tag</th><th>Nacht (22-06h)</th><th>Sonntag</th><th>Feiertag</th>
                        </tr>
                    </thead>
                    <tbody>{stunden_uebersicht_html}</tbody>
                </table>
            </div>

            <div class="grid-2">
                <!-- SCHICHT MANUELL HINZUFÜGEN -->
                <div class="card">
                    <h3>➕ Schicht manuell eintragen</h3>
                    <form action="/schicht-hinzufuegen" method="post">
                        <input type="text" name="datum" placeholder="Datum (z.B. 10.08.2026)" required>
                        <input type="text" name="objekt" placeholder="Objekt (z.B. Maltry)" required>
                        <input type="text" name="zeit" placeholder="Zeit (z.B. 06:00 - 14:00)" required>
                        <input type="text" name="stunden" placeholder="Stunden (z.B. 8)" required>
                        <select name="mitarbeiter">{ma_options_html}</select>
                        <button type="submit">Schicht eintragen</button>
                    </form>
                </div>

                <!-- MITARBEITER VERWALTUNG -->
                <div class="card">
                    <h3>👥 Mitarbeiter-Verwaltung</h3>
                    <table>
                        <thead><tr><th>Name</th><th>Kürzel</th><th>WhatsApp / Handy</th><th>Aktion</th></tr></thead>
                        <tbody>{ma_liste_html}</tbody>
                    </table>
                </div>
            </div>

            <!-- SCHICHTPLAN TABELLE -->
            <div class="card">
                <h3>📅 Aktuelle Schichtplanung</h3>
                <table>
                    <thead>
                        <tr><th>Datum</th><th>Objekt</th><th>Zeit</th><th>Stunden</th><th>Eingeteilt</th><th>Aktion</th></tr>
                    </thead>
                    <tbody>{tabelle_html if tabelle_html else '<tr><td colspan="6" style="text-align:center; color:#94a3b8;">Keine Schichten vorhanden</td></tr>'}</tbody>
                </table>
            </div>

        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


# ==========================================
# WEITERE CHEF ROUTEN & AKTIONEN
# ==========================================

@app.post("/chef-einstellungen")
async def save_einstellungen(max_wochenstunden: int = Form(...)):
    chef_einstellungen["max_wochenstunden"] = max_wochenstunden
    return RedirectResponse(url="/chef-dashboard", status_code=303)

@app.post("/schicht-loeschen/{idx}")
async def schicht_loeschen(idx: int):
    if 0 <= idx < len(aktuelle_schichten):
        aktuelle_schichten.pop(idx)
    return RedirectResponse(url="/chef-dashboard", status_code=303)

@app.post("/mitarbeiter-loeschen/{u_name}")
async def mitarbeiter_loeschen(u_name: str):
    if u_name in USERS and USERS[u_name]["role"] == "mitarbeiter":
        del USERS[u_name]
    return RedirectResponse(url="/chef-dashboard", status_code=303)

@app.post("/schicht-hinzufuegen")
async def schicht_hinzufuegen(
    datum: str = Form(...),
    objekt: str = Form(...),
    zeit: str = Form(...),
    stunden: str = Form(...),
    mitarbeiter: str = Form(...)
):
    aktuelle_schichten.append({
        "datum": datum,
        "objekt": objekt,
        "zeit": zeit,
        "stunden": stunden,
        "mitarbeiter": mitarbeiter
    })
    return RedirectResponse(url="/chef-dashboard", status_code=303)

@app.post("/upload-excel-dienstplan")
async def upload_excel_dienstplan(file: UploadFile = File(...)):
    global aktuelle_schichten
    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        neue_schichten = []
        for _, row in df.iterrows():
            neue_schichten.append({
                "datum": str(row.get("Datum", "")),
                "objekt": str(row.get("Objekt", "")),
                "zeit": str(row.get("Zeit", "")),
                "stunden": str(row.get("Stunden", "8")),
                "mitarbeiter": str(row.get("Mitarbeiter", ""))
            })
        aktuelle_schichten.extend(neue_schichten)
    except Exception as e:
        print(f"Excel-Fehler: {e}")
    return RedirectResponse(url="/chef-dashboard", status_code=303)


# ==========================================
# MITARBEITER DASHBOARD & ZEITERFASSUNG
# ==========================================

@app.get("/mitarbeiter-dashboard", response_class=HTMLResponse)
async def mitarbeiter_dashboard(request: Request, msg: str = ""):
    user = request.cookies.get("session_user")
    if not user or USERS.get(user, {}).get("role") != "mitarbeiter":
        return RedirectResponse(url="/login", status_code=303)

    msg_html = f"<p style='color: #4ade80; text-align:center;'>{msg}</p>" if msg else ""

    # Meine Schichten
    meine_schichten = [s for s in aktuelle_schichten if user in s.get("mitarbeiter", "")]
    schichten_rows = ""
    for s in meine_schichten:
        schichten_rows += f"""
        <tr>
            <td><b>{s.get('datum')}</b></td>
            <td>{s.get('objekt')}</td>
            <td>{s.get('zeit')}</td>
            <td>{s.get('stunden')} Std</td>
        </tr>
        """
    if not schichten_rows:
        schichten_rows = "<tr><td colspan='4' style='text-align:center; color:#94a3b8;'>Keine bevorstehenden Schichten eingeteilt.</td></tr>"

    # Meine Urlaubsanträge
    meine_antraege = [a for a in urlaubs_antraege if a["mitarbeiter"] == user]
    antraege_rows = ""
    for a in meine_antraege:
        color = "#facc15" if a["status"] == "Offen" else ("#4ade80" if a["status"] == "Genehmigt" else "#f87171")
        antraege_rows += f"""
        <tr>
            <td>{a['von']} - {a['bis']}</td>
            <td>{a['grund']}</td>
            <td><span style="color:{color}; font-weight:bold;">{a['status']}</span></td>
        </tr>
        """
    if not antraege_rows:
        antraege_rows = "<tr><td colspan='3' style='text-align:center; color:#94a3b8;'>Bisher keine Urlaubsanträge.</td></tr>"

    # Stempel Status
    user_stempel_info = time_tracking_data.get(user, {})
    stempel_status = user_stempel_info.get("status", "Abgemeldet")
    stempel_btn_text = "🔴 Dienst BEENDEN / Ausstempeln" if stempel_status == "Anwesend" else "🟢 Dienst STARTEN / Einstempeln"
    stempel_action = "ausstempeln" if stempel_status == "Anwesend" else "einstempeln"

    html = f"""
    <!DOCTYPE html>
    <html lang="de">
    <head>
        <meta charset="UTF-8">
        <title>Mitarbeiter Portal - {user}</title>
        <style>
            body {{ font-family: sans-serif; background-color: #0f172a; color: white; padding: 20px; margin:0; }}
            .container {{ max-width: 900px; margin: auto; }}
            header {{ display: flex; justify-content: space-between; align-items: center; background: #1e293b; padding: 15px 25px; border-radius: 12px; border: 1px solid #334155; margin-bottom: 20px; }}
            .card {{ background: #1e293b; padding: 20px; border-radius: 12px; border: 1px solid #334155; margin-bottom: 20px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ padding: 8px 10px; border-bottom: 1px solid #334155; text-align: left; font-size: 13px; }}
            th {{ background: #0f172a; color: #94a3b8; }}
            input, textarea, button {{ background: #0f172a; border: 1px solid #334155; color: white; padding: 10px; border-radius: 6px; width: 100%; box-sizing: border-box; margin-bottom: 10px; font-size: 13px; }}
            button {{ background: #0284c7; font-weight: bold; cursor: pointer; border: none; }}
            .btn-danger {{ background: #e11d48; text-decoration: none; padding: 8px 14px; border-radius: 6px; color: white; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h2 style="margin:0; color:#38bdf8;">👷 Mitarbeiter-Portal | {user}</h2>
                <a href="/logout" class="btn-danger">Abmelden</a>
            </header>
            {msg_html}

            <!-- ZEITERFASSUNG / STEMPELUHR -->
            <div class="card" style="text-align: center;">
                <h3>⏱️ Zeiterfassung (Stempeluhr)</h3>
                <p>Aktueller Status: <b>{stempel_status}</b></p>
                <form action="/stempeln" method="post">
                    <input type="hidden" name="action" value="{stempel_action}">
                    <button type="submit" style="padding: 15px; font-size: 16px; background: {'#e11d48' if stempel_status == 'Anwesend' else '#16a34a'}; font-weight:bold;">
                        {stempel_btn_text}
                    </button>
                </form>
            </div>

            <!-- MEINE SCHICHTEN -->
            <div class="card">
                <h3>📅 Meine eingeteilten Schichten</h3>
                <table>
                    <thead><tr><th>Datum</th><th>Objekt</th><th>Zeit</th><th>Stunden</th></tr></thead>
                    <tbody>{schichten_rows}</tbody>
                </table>
            </div>

            <!-- URLAUB BEANTRAGEN -->
            <div class="card">
                <h3>🌴 Urlaubsantrag einreichen</h3>
                <form action="/urlaub-beantragen" method="post">
                    <div style="display:flex; gap:10px;">
                        <div style="flex:1;">
                            <label style="font-size:11px; color:#94a3b8;">Von:</label>
                            <input type="date" name="von" required>
                        </div>
                        <div style="flex:1;">
                            <label style="font-size:11px; color:#94a3b8;">Bis:</label>
                            <input type="date" name="bis" required>
                        </div>
                    </div>
                    <input type="text" name="grund" placeholder="Grund (z.B. Erholungsurlaub)" required>
                    <button type="submit">Antrag absenden</button>
                </form>
            </div>

            <!-- MEINE URLAUBSANTRAEGE HISTORIE -->
            <div class="card">
                <h3>📜 Meine Urlaubsanträge & Status</h3>
                <table>
                    <thead><tr><th>Zeitraum</th><th>Grund</th><th>Status</th></tr></thead>
                    <tbody>{antraege_rows}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.post("/stempeln")
async def stempeln(request: Request, action: str = Form(...)):
    user = request.cookies.get("session_user")
    if not user or USERS.get(user, {}).get("role") != "mitarbeiter":
        return RedirectResponse(url="/login", status_code=303)

    jetzt = datetime.now()
    now_str = jetzt.strftime("%d.%m.%Y %H:%M")

    user_data = time_tracking_data.get(user, {
        "status": "Abgemeldet",
        "einstempel_zeit": "-",
        "ausstempel_zeit": "-",
        "letzte_dauer": "-"
    })

    if action == "einstempeln":
        user_data["status"] = "Anwesend"
        user_data["einstempel_zeit"] = now_str
        user_data["ausstempel_zeit"] = "-"
        user_data["letzte_dauer"] = "Aktiv..."

        stempel_historie.insert(0, {
            "mitarbeiter": user,
            "aktion": "🟢 Einstempeln (Dienstbeginn)",
            "zeit": now_str,
            "dauer": "-"
        })

    elif action == "ausstempeln":
        user_data["status"] = "Abgemeldet"
        user_data["ausstempel_zeit"] = now_str

        dauer_text = "k.A."
        if user_data.get("einstempel_zeit") and user_data["einstempel_zeit"] != "-":
            try:
                start_dt = datetime.strptime(user_data["einstempel_zeit"], "%d.%m.%Y %H:%M")
                diff = jetzt - start_dt
                stunden, remainder = divmod(diff.seconds, 3600)
                minuten, _ = divmod(remainder, 60)
                dauer_text = f"{diff.days * 24 + stunden} Std {minuten} Min"
            except Exception:
                dauer_text = "Fehler bei Berechnung"

        user_data["letzte_dauer"] = dauer_text

        stempel_historie.insert(0, {
            "mitarbeiter": user,
            "aktion": "🔴 Ausstempeln (Dienstende)",
            "zeit": now_str,
            "dauer": dauer_text
        })

    time_tracking_data[user] = user_data
    return RedirectResponse(url="/mitarbeiter-dashboard?msg=Stempelzeit+erfolgreich+erfasst", status_code=303)


# ==========================================
# ANWENDUNGSSTART
# ==========================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
    