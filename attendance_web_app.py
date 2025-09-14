#!/usr/bin/env python3
"""
Integrated Attendance Web App
Single-file Flask app:
- Admin: create teachers & students (assign student -> teacher)
- Teacher: create session, show session QR, start live webcam scanning (teacher webcam scans student QR)
- Student: see/download their personal QR and view attendance
- Excel export & optional email sending
"""

from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import io, base64, os, secrets
from PIL import Image
import numpy as np
import pandas as pd
import qrcode
from qrcode.constants import ERROR_CORRECT_H
import cv2
from pyzbar.pyzbar import decode as zbar_decode
from flask_mail import Mail, Message

# -------------------------
# Configuration
# -------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('ATTEND_SECRET', 'change-this-secret')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///attendance.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Optional email config (for export sending). Set env vars or change here.
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 465))
app.config['MAIL_USE_SSL'] = True if os.environ.get('MAIL_USE_SSL', 'True') == 'True' else False
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')

db = SQLAlchemy(app)
mail = Mail(app)

# -------------------------
# Models
# -------------------------
class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    user_type = db.Column(db.String(20), nullable=False)  # admin, teacher, student
    name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # For students: assigned teacher (optional)
    assigned_teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    # relationship for teacher -> students
    students = db.relationship('User', lazy='select', backref=db.backref('assigned_teacher', remote_side=[id]), foreign_keys=[assigned_teacher_id])

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Session(db.Model):
    __tablename__ = 'session'
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    session_name = db.Column(db.String(200), nullable=False)
    qr_token = db.Column(db.String(100), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)

    teacher = db.relationship('User', backref='sessions', foreign_keys=[teacher_id])

class Attendance(db.Model):
    __tablename__ = 'attendance'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    qr_data = db.Column(db.Text, nullable=True)

    student = db.relationship('User', foreign_keys=[student_id])
    teacher = db.relationship('User', foreign_keys=[teacher_id])
    session = db.relationship('Session', foreign_keys=[session_id])

# -------------------------
# DB init and default admin
# -------------------------
with app.app_context():
    db.create_all()
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(username='admin', email='admin@example.com', user_type='admin', name='Administrator')
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()

# -------------------------
# Helpers
# -------------------------
def login_user(user):
    session['user_id'] = user.id
    session['username'] = user.username
    session['user_type'] = user.user_type
    session['name'] = user.name

def logout_user():
    session.clear()

def generate_qr_image_data_uri(data):
    qr = qrcode.QRCode(version=1, error_correction=ERROR_CORRECT_H, box_size=8, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode()
    return f"data:image/png;base64,{encoded}"

def decode_qr_from_b64_image(b64data):
    """b64data is a base64 string (data:image/png;base64,...) from client camera snapshot"""
    try:
        if ',' in b64data:
            b64data = b64data.split(',', 1)[1]
        data = base64.b64decode(b64data)
        img = Image.open(io.BytesIO(data)).convert('RGB')
        arr = np.array(img)[:, :, ::-1]  # RGB -> BGR
        decoded = zbar_decode(arr)
        if decoded:
            return decoded[0].data.decode('utf-8')
    except Exception as e:
        app.logger.debug(f"QR decode error: {e}")
    return None

def export_attendance_df_for_teacher(teacher_id):
    """Return pandas DataFrame of attendance for teacher"""
    q = db.session.query(Attendance).filter(Attendance.teacher_id == teacher_id).order_by(Attendance.timestamp.desc()).all()
    rows = []
    for r in q:
        rows.append({
            "Student": r.student.name,
            "Student Username": r.student.username,
            "Session": r.session.session_name if r.session else str(r.session_id),
            "Timestamp": r.timestamp,
        })
    df = pd.DataFrame(rows)
    return df

def send_excel_via_email(to_email, df, subject="Attendance Report"):
    """Try to send dataframe as excel attachment using Flask-Mail. Returns (ok, message)."""
    if not app.config.get('MAIL_USERNAME') or not app.config.get('MAIL_PASSWORD'):
        return False, "Mail server not configured"
    try:
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine='openpyxl')
        buf.seek(0)
        msg = Message(subject=subject, sender=app.config['MAIL_USERNAME'], recipients=[to_email])
        msg.body = "Attached attendance export"
        msg.attach("attendance.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buf.read())
        mail.send(msg)
        return True, "Email sent"
    except Exception as e:
        return False, str(e)

# -------------------------
# TEMPLATES (complete HTML)
# -------------------------

base_template = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body{{font-family:system-ui,Segoe UI,Roboto,Helvetica,Arial; max-width:1100px;margin:1.5rem auto;padding:1rem;}}
    a{{color: #0a66c2}}
    .nav{{margin-bottom:1rem}}
    .card{{border:1px solid #ddd;padding:1rem;border-radius:8px;margin-bottom:1rem}}
    .qr-large img{{width:320px}}
    table{{border-collapse:collapse;width:100%}}
    th,td{{border:1px solid #ccc;padding:.4rem;text-align:left}}
    .success{{color:green}}.error{{color:red}}
    button{{padding:.5rem 1rem;border-radius:6px}}
  </style>
</head>
<body>
  <div class="nav">
    {nav}
  </div>
  {messages}
  {body}
</body>
</html>
"""

def render_page(title, body, nav_html="", messages_html=""):
    return base_template.format(
        title=title,
        nav=nav_html,
        messages=messages_html,
        body=body
    )

def get_nav_html():
    if 'user_id' in session:
        nav = f"Hello {session['name']} ({session['user_type']}) — <a href=\"{url_for('logout')}\">Logout</a>"
        if session['user_type'] == 'admin':
            nav += f" | <a href=\"{url_for('admin_dashboard')}\">Admin</a>"
        elif session['user_type'] == 'teacher':
            nav += f" | <a href=\"{url_for('teacher_dashboard')}\">Teacher</a>"
        elif session['user_type'] == 'student':
            nav += f" | <a href=\"{url_for('student_dashboard')}\">Student</a>"
        return nav
    else:
        return f"<a href=\"{url_for('index')}\">Home</a> | <a href=\"{url_for('login')}\">Login</a>"

def get_messages_html():
    messages = []
    for cat, msg in session.pop('_flashes', []):
        css_class = 'success' if cat == 'success' else 'error'
        messages.append(f'<div class="{css_class}">{msg}</div>')
    return ''.join(messages)

# -------------------------
# Routes
# -------------------------
@app.route('/')
def index():
    body = """
    <div class="card">
        <h2>Attendance System</h2>
        <p>Login to use the system. Default admin: <b>admin/admin123</b>.</p>
    </div>
    """
    return render_page("Attendance System", body, get_nav_html(), get_messages_html())

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user_type = request.form.get('user_type', '')
        user = User.query.filter_by(username=username, user_type=user_type).first()
        if user and user.check_password(password):
            login_user(user)
            flash('Logged in', 'success')
            if user.user_type == 'admin':
                return redirect(url_for('admin_dashboard'))
            if user.user_type == 'teacher':
                return redirect(url_for('teacher_dashboard'))
            if user.user_type == 'student':
                return redirect(url_for('student_dashboard'))
        else:
            flash('Invalid credentials', 'error')
    
    body = """
    <div class="card">
        <h3>Login</h3>
        <form method="post">
            <div><label>Username <input name="username" required></label></div>
            <div><label>Password <input type="password" name="password" required></label></div>
            <div><label>User type
                <select name="user_type">
                    <option value="admin">Admin</option>
                    <option value="teacher">Teacher</option>
                    <option value="student">Student</option>
                </select>
            </label></div>
            <div style="margin-top:.5rem"><button type="submit">Login</button></div>
        </form>
    </div>
    """
    return render_page("Login", body, get_nav_html(), get_messages_html())

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))

# -------------------------
# Admin: manage users
# -------------------------
@app.route('/admin')
def admin_dashboard():
    if session.get('user_type') != 'admin':
        return redirect(url_for('login'))
    teachers = User.query.filter_by(user_type='teacher').all()
    students = User.query.filter_by(user_type='student').all()
    
    teachers_html = ""
    for t in teachers:
        teachers_html += f"<tr><td>{t.name}</td><td>{t.username}</td><td>{t.email}</td></tr>"
    
    students_html = ""
    for s in students:
        teacher_name = s.assigned_teacher.name if s.assigned_teacher else ''
        students_html += f"<tr><td>{s.name}</td><td>{s.username}</td><td>{s.email}</td><td>{teacher_name}</td></tr>"
    
    body = f"""
    <div class="card">
        <h3>Admin Dashboard</h3>
        <p><a href="{url_for('admin_add_user')}">Add user (teacher / student)</a> | <a href="{url_for('admin_export_all')}">Export all attendance (Excel)</a></p>
        <h4>Teachers</h4>
        <table>
            <tr><th>Name</th><th>Username</th><th>Email</th></tr>
            {teachers_html}
        </table>
        <h4>Students</h4>
        <table>
            <tr><th>Name</th><th>Username</th><th>Email</th><th>Assigned Teacher</th></tr>
            {students_html}
        </table>
    </div>
    """
    return render_page("Admin Dashboard", body, get_nav_html(), get_messages_html())

@app.route('/admin/add_user', methods=['GET', 'POST'])
def admin_add_user():
    if session.get('user_type') != 'admin':
        return redirect(url_for('login'))
    teachers = User.query.filter_by(user_type='teacher').all()
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip()
        name = request.form['name'].strip()
        password = request.form['password']
        user_type = request.form['user_type']
        assigned_teacher_id = request.form.get('assigned_teacher') or None
        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash('Username or email exists', 'error')
            return redirect(url_for('admin_add_user'))
        u = User(username=username, email=email, user_type=user_type, name=name)
        u.set_password(password)
        if user_type == 'student' and assigned_teacher_id:
            u.assigned_teacher_id = int(assigned_teacher_id)
        db.session.add(u)
        db.session.commit()
        flash('User created', 'success')
        return redirect(url_for('admin_dashboard'))
    
    teacher_options = ""
    for t in teachers:
        teacher_options += f'<option value="{t.id}">{t.name} ({t.username})</option>'
    
    body = f"""
    <div class="card">
        <h3>Add user</h3>
        <form method="post">
            <div><label>Name <input name="name" required></label></div>
            <div><label>Username <input name="username" required></label></div>
            <div><label>Email <input name="email" type="email" required></label></div>
            <div><label>Password <input name="password" type="password" required></label></div>
            <div><label>User type
                <select name="user_type" id="user_type" onchange="document.getElementById('teacher-select').style.display = (this.value=='student') ? 'block' : 'none'">
                    <option value="teacher">Teacher</option>
                    <option value="student">Student</option>
                </select>
            </label></div>
            <div id="teacher-select" style="display:none">
                <label>Assign Teacher
                    <select name="assigned_teacher">
                        <option value="">-- none --</option>
                        {teacher_options}
                    </select>
                </label>
            </div>
            <div style="margin-top:.5rem"><button type="submit">Create</button></div>
        </form>
    </div>
    """
    return render_page("Add User", body, get_nav_html(), get_messages_html())

@app.route('/admin/export_all')
def admin_export_all():
    if session.get('user_type') != 'admin':
        return redirect(url_for('login'))
    # Query all attendance
    q = db.session.query(Attendance).order_by(Attendance.timestamp.desc()).all()
    rows = []
    for r in q:
        rows.append({
            "Student": r.student.name,
            "Student Username": r.student.username,
            "Teacher": r.teacher.name if r.teacher else '',
            "Session": r.session.session_name if r.session else '',
            "Timestamp": r.timestamp
        })
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine='openpyxl')
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='attendance_all.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

# -------------------------
# Teacher flows
# -------------------------
@app.route('/teacher')
def teacher_dashboard():
    if session.get('user_type') != 'teacher':
        return redirect(url_for('login'))
    teacher = User.query.get(session['user_id'])
    sessions = Session.query.filter_by(teacher_id=teacher.id).order_by(Session.created_at.desc()).all()
    
    sessions_html = ""
    for s in sessions:
        active = 'Yes' if s.is_active else 'No'
        sessions_html += f"""
        <tr>
            <td>{s.session_name}</td>
            <td>{active}</td>
            <td>{s.expires_at}</td>
            <td><a href="{url_for('teacher_session', session_id=s.id)}">Open</a></td>
        </tr>
        """
    
    body = f"""
    <div class="card">
        <h3>Teacher Dashboard</h3>
        <p><a href="{url_for('teacher_create_session')}">Create Session</a> | <a href="{url_for('teacher_export')}">Export my attendance</a></p>
        <h4>Your sessions</h4>
        <table>
            <tr><th>Session</th><th>Active</th><th>Expires</th><th>Open</th></tr>
            {sessions_html}
        </table>
    </div>
    """
    return render_page("Teacher Dashboard", body, get_nav_html(), get_messages_html())

@app.route('/teacher/create_session', methods=['GET', 'POST'])
def teacher_create_session():
    if session.get('user_type') != 'teacher':
        return redirect(url_for('login'))
    if request.method == 'POST':
        session_name = request.form.get('session_name') or f"Session {datetime.utcnow().isoformat()}"
        duration = int(request.form.get('duration_hours') or 1)
        token = secrets.token_hex(12)
        expires_at = datetime.utcnow() + timedelta(hours=duration)
        s = Session(teacher_id=session['user_id'], session_name=session_name, qr_token=token, expires_at=expires_at)
        db.session.add(s)
        db.session.commit()
        flash('Session created', 'success')
        return redirect(url_for('teacher_dashboard'))
    
    body = """
    <div class="card">
        <h3>Create Session</h3>
        <form method="post">
            <div><label>Session name <input name="session_name" required></label></div>
            <div><label>Duration (hours) <input name="duration_hours" type="number" value="1" min="1" required></label></div>
            <div style="margin-top:.5rem"><button>Create</button></div>
        </form>
    </div>
    """
    return render_page("Create Session", body, get_nav_html(), get_messages_html())

@app.route('/teacher/session/<int:session_id>')
def teacher_session(session_id):
    if session.get('user_type') != 'teacher':
        return redirect(url_for('login'))
    s = Session.query.get_or_404(session_id)
    if s.teacher_id != session['user_id']:
        flash('Not your session', 'error')
        return redirect(url_for('teacher_dashboard'))

    # Prepare session QR to show teacher (if desired)
    session_qr = generate_qr_image_data_uri(f"TEACHER_TOKEN:{s.qr_token}")
    # session attendance
    attendance = Attendance.query.filter_by(session_id=s.id).order_by(Attendance.timestamp.desc()).all()
    
    attendance_html = ""
    for a in attendance:
        attendance_html += f"<tr><td>{a.timestamp}</td><td>{a.student.name}</td><td>{a.student.username}</td></tr>"
    
    body = f"""
    <div class="card">
        <h3>Session: {s.session_name}</h3>
        <div style="display:flex;gap:2rem;">
            <div class="qr-large">
                <h4>Session QR (can be scanned by students if you want)</h4>
                <img src="{session_qr}">
            </div>
            <div>
                <h4>Live Scanner (teacher's webcam reads Student QR)</h4>
                <video id="video" autoplay playsinline width="400" style="border:1px solid #ccc"></video>
                <div style="margin-top:.5rem">
                    <button id="startBtn">Start Scanning</button>
                    <button id="stopBtn" disabled>Stop</button>
                </div>
                <div id="scanLog" style="margin-top:.5rem"></div>
            </div>
        </div>

        <h4 style="margin-top:1rem">Attendance recorded ({len(attendance)})</h4>
        <table>
            <tr><th>Timestamp</th><th>Student</th><th>Username</th></tr>
            {attendance_html}
        </table>
        <p><a href="{url_for('teacher_export')}">Export My Attendance (Excel)</a></p>
    </div>

<script>
const video = document.getElementById('video');
let stream = null;
let scanning = false;
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const scanLog = document.getElementById('scanLog');

async function startCamera(){{
  try {{
    stream = await navigator.mediaDevices.getUserMedia({{ video: {{ facingMode: 'environment' }}, audio: false }});
    video.srcObject = stream;
    scanning = true;
    startBtn.disabled = true;
    stopBtn.disabled = false;
    captureLoop();
  }} catch (e) {{
    alert('Camera access denied or not available: ' + e.message);
  }}
}}
function stopCamera(){{
  if(stream){{ stream.getTracks().forEach(t=>t.stop()); stream=null; }}
  scanning = false;
  startBtn.disabled = false;
  stopBtn.disabled = true;
}}
startBtn.onclick = startCamera;
stopBtn.onclick = stopCamera;

async function captureLoop(){{
  // capture a frame every ~600ms and send to server for QR decode
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  while(scanning){{
    try{{
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const dataUrl = canvas.toDataURL('image/png');
      // send to server
      const resp = await fetch('{url_for("teacher_scan_frame", session_id=s.id)}', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{image_data: dataUrl}})
      }});
      const j = await resp.json();
      if(j.found){{
        scanLog.innerHTML = `<div>Found student QR: ${{j.qr_data}} — ${{j.message || ''}}</div>` + scanLog.innerHTML;
      }}
    }}catch(e){{
      console.error(e);
    }}
    await new Promise(r=>setTimeout(r, 600));
  }}
}}
</script>
    """
    return render_page(f"Session: {s.session_name}", body, get_nav_html(), get_messages_html())

@app.route('/teacher/scan_frame/<int:session_id>', methods=['POST'])
def teacher_scan_frame(session_id):
    """Receive base64 image from teacher browser, decode QR, mark attendance if student QR"""
    if session.get('user_type') != 'teacher':
        return jsonify({'found': False, 'error': 'unauth'}), 403
    s = Session.query.get_or_404(session_id)
    if s.teacher_id != session['user_id']:
        return jsonify({'found': False, 'error': 'not your session'}), 403
    payload = request.get_json(silent=True) or {}
    img_b64 = payload.get('image_data')
    if not img_b64:
        return jsonify({'found': False})
    qr = decode_qr_from_b64_image(img_b64)
    if not qr:
        return jsonify({'found': False})
    # Expect student QR format: STUDENT:<username>:<name>
    if qr.startswith('STUDENT:'):
        try:
            _, username, name = qr.split(':', 2)
        except ValueError:
            return jsonify({'found': False})
        student = User.query.filter_by(username=username, user_type='student').first()
        if not student:
            return jsonify({'found': False, 'message': 'Unknown student'})
        # Check if already present in this session
        existing = Attendance.query.filter_by(session_id=s.id, student_id=student.id).first()
        if existing:
            return jsonify({'found': True, 'qr_data': qr, 'message': 'Already recorded'})
        att = Attendance(student_id=student.id, teacher_id=s.teacher_id, session_id=s.id, qr_data=qr)
        db.session.add(att)
        db.session.commit()
        return jsonify({'found': True, 'qr_data': qr, 'message': 'Attendance recorded'})
    return jsonify({'found': False, 'message': 'Not a student QR'})

@app.route('/teacher/export')
def teacher_export():
    if session.get('user_type') != 'teacher':
        return redirect(url_for('login'))
    teacher_id = session['user_id']
    df = export_attendance_df_for_teacher(teacher_id)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine='openpyxl')
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='my_attendance.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/teacher/email_export', methods=['POST'])
def teacher_email_export():
    if session.get('user_type') != 'teacher':
        return redirect(url_for('login'))
    to_email = request.form.get('to_email')
    if not to_email:
        flash('Provide email', 'error')
        return redirect(url_for('teacher_dashboard'))
    df = export_attendance_df_for_teacher(session['user_id'])
    ok, msg = send_excel_via_email(to_email, df, subject=f"Attendance export - {session['username']}")
    flash(msg, 'success' if ok else 'error')
    return redirect(url_for('teacher_dashboard'))

# -------------------------
# Student flows
# -------------------------
@app.route('/student')
def student_dashboard():
    if session.get('user_type') != 'student':
        return redirect(url_for('login'))
    student = User.query.get(session['user_id'])
    # student QR data
    student_data = f"STUDENT:{student.username}:{student.name}"
    student_qr = generate_qr_image_data_uri(student_data)
    # their attendance
    records = Attendance.query.filter_by(student_id=student.id).order_by(Attendance.timestamp.desc()).all()
    
    records_html = ""
    for r in records:
        session_name = r.session.session_name if r.session else ''
        teacher_name = r.teacher.name if r.teacher else ''
        records_html += f"<tr><td>{r.timestamp}</td><td>{session_name}</td><td>{teacher_name}</td></tr>"
    
    body = f"""
    <div class="card">
        <h3>Student Dashboard</h3>
        <div style="display:flex;gap:2rem">
            <div class="qr-large">
                <h4>Your Student QR</h4>
                <img src="{student_qr}">
                <p>Save this image or open on your phone to get scanned by teacher's webcam.</p>
            </div>
            <div>
                <h4>Your Attendance</h4>
                <table>
                    <tr><th>Timestamp</th><th>Session</th><th>Teacher</th></tr>
                    {records_html}
                </table>
            </div>
        </div>
    </div>
    """
    return render_page("Student Dashboard", body, get_nav_html(), get_messages_html())

# -------------------------
# API: optional quick student self-scan (if you want students to scan teacher QR using phone)
# -------------------------
@app.route('/student/scan_teacher', methods=['POST'])
def student_scan_teacher():
    """Alternate flow: student scans teacher QR (if teacher shows QR and student scans with phone and uploads)"""
    if session.get('user_type') != 'student':
        return jsonify({'success': False, 'message': 'unauth'}), 403
    image_data = (request.json or {}).get('image_data')
    qr = decode_qr_from_b64_image(image_data) if image_data else None
    if not qr:
        return jsonify({'success': False, 'message': 'No QR'})
    if qr.startswith('TEACHER_TOKEN:'):
        token = qr.split(':', 1)[1]
        s = Session.query.filter_by(qr_token=token, is_active=True).first()
        if not s or s.expires_at < datetime.utcnow():
            return jsonify({'success': False, 'message': 'Invalid/expired session'})
        # mark attendance
        existing = Attendance.query.filter_by(session_id=s.id, student_id=session['user_id']).first()
        if existing:
            return jsonify({'success': False, 'message': 'Already recorded'})
        att = Attendance(student_id=session['user_id'], teacher_id=s.teacher_id, session_id=s.id, qr_data=qr)
        db.session.add(att)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Attendance marked', 'session_name': s.session_name})
    return jsonify({'success': False, 'message': 'Not a teacher token'})

# -------------------------
# Misc: static simple health check
# -------------------------
@app.route('/ping')
def ping():
    return "pong"

# -------------------------
# Run
# -------------------------
if __name__ == '__main__':
    # For local dev only. For production use a WSGI server (gunicorn / uvicorn + nginx).
    app.run(debug=True, host='0.0.0.0', port=5000)
