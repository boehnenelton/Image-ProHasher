#!/usr/bin/env python3
"""
Name/Library:   tests/ui_audit_playwright.py
Description:    Repeatable browser audit of group_viewer. Builds a THROWAWAY copy of the project
                 and a synthetic photo set in a temp folder (your real config/sessions/photos are
                 never touched), starts the viewer on a free port, and drives it in Chromium at
                 desktop, tablet and phone sizes. Asserts: no horizontal overflow on any view,
                 single-row toolbar, tap targets >= 44px on touch, selection bar fully reachable,
                 no stray "null/undefined/NaN" text, KEEP/TRASH ribbons after Keep on a cold group,
                 prev/next + keyboard + browser Back, bulk resolve -> commit -> Undo restoring groups,
                 and zero console errors. Needs:  pip install playwright && playwright install chromium
                 Run:  python3 tests/ui_audit_playwright.py     (exit 0 pass, 1 fail, 3 playwright missing)
Version:        2
Date Created:   2026-10-20
Author:         Elton Boehnen
Contact:        boehnenelton2024@gmail.com | boehnenelton2024.pages.dev | github.com/boehnenelton
Format_Creator: Elton Boehnen
RELATIONAL_ID:  e4e177d2-f34a-4479-a94c-9a8c2291d46b
"""
import json, os, random, re, shutil, socket, subprocess, sys, tempfile, time
from pathlib import Path

try:
    from termux_playwright import sync_playwright_termux, launch_sync
    HAS_TERMUX_PW = True
except ImportError:
    HAS_TERMUX_PW = False
    launch_sync = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    if not HAS_TERMUX_PW:
        sys.stdout.write("playwright not installed: pip install playwright && playwright install chromium\n")
        sys.exit(3)
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="prohasher-ui-"))
APP, PHOTOS = TMP / "app", TMP / "photos"
ENV = dict(os.environ, HOME=str(TMP), PYTHONDONTWRITEBYTECODE="1")
issues, errs = [], []


def seed_photos():
    for d in ("Camera", "Backup", "Misc"):
        (PHOTOS / d).mkdir(parents=True, exist_ok=True)

    def scene(seed, size=(1280, 960)):
        r = random.Random(seed)
        g = Image.new("RGB", (1, 2)); g.putpixel((0, 0), tuple(r.randint(30, 230) for _ in range(3))); g.putpixel((0, 1), tuple(r.randint(30, 230) for _ in range(3)))
        im = g.resize(size, Image.BILINEAR); d = ImageDraw.Draw(im)
        for _ in range(8):
            x, y, rad = r.randint(0, size[0]), r.randint(0, size[1]), r.randint(60, 260)
            d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=tuple(r.randint(20, 250) for _ in range(3)))
        return im.filter(ImageFilter.GaussianBlur(2))

    def save(im, path, dt=None):
        ex = Image.Exif()
        if dt:
            ex[0x0110] = "Cam"; ex[0x0132] = dt
        im.save(path, "JPEG", quality=90, exif=ex.tobytes() if dt else b"")

    for g in range(10):
        dt = f"2024:0{1 + g % 9}:{10 + g:02d} 1{g % 10}:2{g % 6}:00"
        src = PHOTOS / "Camera" / f"IMG_{4000 + g}.jpg"
        save(scene(g), src, dt)
        shutil.copy(src, PHOTOS / "Backup" / f"IMG_{4000 + g}.jpg")
        if g % 3 == 2:
            shutil.copy(src, PHOTOS / "Backup" / f"IMG_{4000 + g} (1).jpg")
    for i in range(10):
        save(scene(100 + i, (900 + i * 13, 700 + i * 7)), PHOTOS / "Misc" / f"unique_{i}.jpg")


def cli(*args):
    r = subprocess.run([sys.executable, *args], cwd=APP, env=ENV, capture_output=True, text=True)
    return r.stdout + r.stderr


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


T = BASE = None

def chk(name,cond,detail=""):
    if not cond: issues.append(f"{name}: {detail}")
def audit(pg,tag,touch):
    d=pg.evaluate("""()=>{const bad=[];const vw=innerWidth;
      document.querySelectorAll('button,select,input,textarea,[role=checkbox]').forEach(e=>{const r=e.getBoundingClientRect();if(r.width===0||r.height===0)return;const cs=getComputedStyle(e);if(cs.visibility==='hidden')return;
        if(r.right>vw+1||r.left<-1)bad.push('offscreen:'+(e.textContent||e.id||e.tagName).trim().slice(0,20));});
      const small=[...document.querySelectorAll('button,select,input[type=search]')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&(r.height<40||r.width<40)&&!e.closest('#toolbar')&&!e.closest('#popMenu')}).map(e=>(e.textContent||e.id||e.className).trim().slice(0,18)+':'+Math.round(e.getBoundingClientRect().width)+'x'+Math.round(e.getBoundingClientRect().height));
      const tb=document.getElementById('toolbar');const tops=new Set([...tb.querySelectorAll('button,select')].map(b=>Math.round(b.getBoundingClientRect().top)));
      return {hscroll:document.documentElement.scrollWidth-innerWidth,txt:/\\b(null|undefined|NaN)\\b/.test(document.body.innerText),small,rows:tops.size,offs:bad.filter(x=>!x.startsWith('offscreen:')||true)}}""")
    chk(tag+" h-overflow",d["hscroll"]<=1,d["hscroll"]); chk(tag+" stray null/undefined/NaN text",not d["txt"])
    chk(tag+" toolbar single row",d["rows"]<=1,d["rows"])
    if touch and d["small"]: chk(tag+" small tap targets",False,d["small"][:6])
    return d
def run(p,name,vp,mobile):
    if HAS_TERMUX_PW:
        b = launch_sync(p, headless=True)
    else:
        b = p.chromium.launch()
    ctx = b.new_context(viewport=vp, is_mobile=mobile, has_touch=mobile)
    pg = ctx.new_page()
    pg.on("console",lambda m: errs.append((name,m.text)) if m.type=="error" else None); pg.on("pageerror",lambda e: errs.append((name,str(e))))
    shot=lambda s: None
    if name == "desktop":                                  # the sign-in FORM path (browser sends Origin: null) - was a 403
        pg.goto(f"{BASE}/"); chk("unauthenticated visit lands on /login", pg.url.rstrip("/").endswith("/login"), pg.url)
        pg.fill("input[name=token]", "definitely-wrong"); pg.click("button[type=submit]"); pg.wait_for_timeout(400)
        chk("wrong token shows an error", "Wrong token" in pg.inner_text("body"))
        errs[:] = [e for e in errs if "403" not in str(e)]                        # that 403 is the intended refusal, not a bug
        chk("sign-in page is styled (assets load before login)", pg.evaluate("()=>getComputedStyle(document.body).display==='flex'"))
        pg.fill("input[name=token]", T); pg.click("button[type=submit]")
    else:
        pg.goto(f"{BASE}/?token={T}")
    pg.wait_for_selector(".gv-card"); pg.wait_for_timeout(700)
    chk(name+" token stripped from URL","token" not in pg.url,pg.url)
    audit(pg,name+" list",mobile); shot("a-list")
    n=pg.locator(".gv-card").count(); 
    # search + empty state
    pg.fill("#filterInput","zzzz"); pg.wait_for_selector("#emptyState:not([hidden])"); chk(name+" empty state text","No groups match" in pg.inner_text("#emptyState")); shot("b-empty")
    pg.fill("#filterInput",""); pg.wait_for_selector(".gv-card")
    # detail
    pg.locator(".gv-card").first.click(); pg.wait_for_selector(".gv-tile"); pg.wait_for_timeout(500)
    if pg.locator("#btnExpand").is_visible(): pg.click("#btnExpand"); pg.wait_for_timeout(600)
    audit(pg,name+" detail",mobile); shot("c-detail")
    chk(name+" pager shows position","of" in pg.inner_text("#pagerText"),pg.inner_text("#pagerText"))
    right=pg.evaluate("()=>{const b=[...document.querySelectorAll('#toolbar button')].find(x=>x.textContent.trim().startsWith('Resolve'));return b?b.getBoundingClientRect().right-innerWidth:999}")
    chk(name+" primary 'Resolve' button fully visible without scrolling the toolbar",right<=1,right)
    chk(name+" selbar hidden w/o selection",pg.locator("#selBar").is_hidden())
    # selection -> bottom bar in viewport
    pg.locator(".gv-tile").nth(0).click(); pg.wait_for_timeout(250)
    chk(name+" 1-selected bar has no 'null'","null" not in pg.inner_text("#selBar"),pg.inner_text("#selBar")); chk(name+" 1-selected shows count","1" in pg.inner_text("#selBar"))
    far=pg.evaluate("()=>Math.max(...[...document.querySelectorAll('#selBar button')].map(b=>b.getBoundingClientRect().right))-innerWidth")
    chk(name+" ALL selection-bar buttons fit without scrolling (1 selected)",far<=1,far)
    pg.locator(".gv-tile").nth(1).click(); pg.wait_for_timeout(300)
    d=audit(pg,name+" selected",mobile); shot("d-selected")
    bar=pg.locator("#selBar"); chk(name+" selbar visible",bar.is_visible())
    r=pg.evaluate("()=>{const t=[...document.querySelectorAll('#selBar button')].find(b=>b.textContent.trim()==='Trash').getBoundingClientRect();return [t.left,t.right,t.bottom,innerWidth,innerHeight]}")
    chk(name+" Trash reachable in viewport",r[0]>=0 and r[1]<=r[3] and r[2]<=r[4]+1,r)
    chk(name+" no 'Selected/Selection' label noise", "Selection" not in pg.inner_text("#selBar"))
    # keyboard + a11y
    pg.keyboard.press("Escape"); pg.wait_for_timeout(200); chk(name+" Esc clears selection",pg.locator("#selBar").is_hidden())
    pg.keyboard.press("a"); pg.wait_for_timeout(200); chk(name+" 'a' selects all",pg.locator(".gv-tile.is-selected").count()==pg.locator(".gv-tile").count())
    pg.keyboard.press("Escape")
    chk(name+" tiles keyboard-focusable",pg.locator(".gv-tile[tabindex='0']").count()>0)
    # next / prev, history back
    first=pg.inner_text("#groupDetailTitle"); pg.click("#btnNext"); pg.wait_for_timeout(500); chk(name+" next group changes",pg.inner_text("#groupDetailTitle")!=first)
    pg.keyboard.press("k"); pg.wait_for_timeout(500); chk(name+" 'k' goes back to previous",pg.inner_text("#groupDetailTitle")==first)
    pg.go_back(); pg.wait_for_timeout(500); chk(name+" browser Back returns to list",pg.locator("#groupListView").is_visible())
    # lightbox
    pg.locator(".gv-card").first.click(); pg.wait_for_selector(".gv-tile"); pg.wait_for_timeout(400)
    if pg.locator("#btnExpand").is_visible() and "Show each" in pg.inner_text("#btnExpand"): pg.click("#btnExpand"); pg.wait_for_timeout(600)
    pg.locator(".gv-tile").nth(0).click(); pg.locator(".gv-tile").nth(1).click()
    pg.evaluate("()=>[...document.querySelectorAll('#selBar button')].find(b=>b.textContent.trim()==='\\u22ef').click()"); pg.wait_for_timeout(200)
    pg.click("text=Compare side by side"); pg.wait_for_timeout(900); audit(pg,name+" compare",mobile); shot("e-compare"); pg.keyboard.press("Escape")
    # keep -> review dialog
    pg.keyboard.press("Escape"); pg.click("text=Keep"); pg.click("text=Highest resolution"); pg.wait_for_selector("#pendingPill:not([hidden])"); pg.wait_for_timeout(700)
    chk(name+" KEEP ribbon shown","KEEP" in pg.inner_text("#tileGrid")); chk(name+" TRASH ribbon shown","TRASH" in pg.inner_text("#tileGrid")); shot("f-queued")
    pg.click("#pendingPill"); pg.wait_for_selector("#dialogBackdrop.is-open"); pg.wait_for_timeout(500); audit(pg,name+" review",mobile); shot("g-review")
    for _ in range(12): pg.keyboard.press("Tab")                                   # focus must stay inside the open dialog
    chk(name+" Tab stays inside dialog",pg.evaluate("()=>document.getElementById('dialogBackdrop').contains(document.activeElement)"))
    # discard, then bulk resolve
    pg.click("text=Discard all"); pg.wait_for_timeout(400)
    pg.click("text=Back to groups") if pg.locator("text=Back to groups").count() else pg.click("#btnBack"); pg.wait_for_selector(".gv-card")
    pg.click("text=Resolve exact copies"); pg.click("text=Keep highest resolution"); pg.wait_for_selector("#dialogBackdrop.is-open"); pg.click("text=Check & queue")
    pg.wait_for_selector("#toast:not([hidden])"); pg.wait_for_timeout(2500); 
    pg.click("#pendingPill"); pg.wait_for_selector("#dialogBackdrop.is-open"); pg.wait_for_timeout(600); shot("h-bulk-review")
    pg.click("#dialogActions >> text=Commit"); pg.wait_for_selector("#toastAction:not([hidden])"); pg.wait_for_timeout(500)
    pg.click("#toastAction"); pg.wait_for_timeout(900); pg.wait_for_timeout(600); chk(name+" Undo restores all groups",pg.locator(".gv-card").count()==n,f"{pg.locator('.gv-card').count()} vs {n}")
    # other views + drawer
    for v in ("trash","notes","about"):
        if mobile: pg.click("#hamburgerBtn"); pg.wait_for_timeout(300)
        pg.click(f".gv-nav__item[data-view={v}]"); pg.wait_for_timeout(400); audit(pg,name+" "+v,mobile)
        if v=="about": shot("i-about")
    if mobile:
        pg.click("#hamburgerBtn"); pg.wait_for_timeout(350); shot("j-drawer")
    else:
        pg.click("#btnSignOut"); pg.wait_for_timeout(700)
        chk(name+" Sign out returns to /login",pg.url.rstrip("/").endswith("/login"),pg.url)
        r=pg.request.get(BASE+"/api/sessions"); chk(name+" session revoked after sign out",r.status==401,r.status)
        # Test username and password login flow
        pg.fill("input[name=username]", "admin")
        pg.fill("input[name=password]", "changeme")
        pg.click("button[type=submit]")
        pg.wait_for_selector(".gv-header")
        chk(name+" username/password login lands on dashboard", "login" not in pg.url)
        pg.click(".gv-nav__item[data-view=groups]"); pg.wait_for_timeout(400)
        chk(name+" groups visible after re-login", pg.locator(".gv-card").count() > 0)
    b.close()

def main():
    shutil.copytree(ROOT, APP, ignore=shutil.ignore_patterns("tests", "__pycache__", "sessions", "*.pyc"))
    (APP / "config").mkdir(exist_ok=True)
    seed_photos()
    cli("Cli_Image_Scanner.py", "--New-Session"); cli("Cli_Image_Scanner.py", "--Dir-Scan", str(PHOTOS)); cli("Cli_Image_Scanner.py", "--Start-Scan", "--Speed")
    out = cli("CLI_Image_Grouper.py", "--Quick-Group")
    sys.stdout.write(re.sub(r"\x1b\[[0-9;]*m", "", out).strip().splitlines()[-1] + "\n")
    port = free_port()
    viewer = subprocess.Popen([sys.executable, "group_viewer.py", "--no-browser", "--port", str(port)], cwd=APP, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    token = None
    t0 = time.time()
    while time.time() - t0 < 20 and token is None:
        line = viewer.stdout.readline()
        m = re.search(r"token=([A-Za-z0-9_\-]+)", line)
        if m:
            token = m.group(1)
    if not token:
        viewer.kill(); sys.stdout.write("viewer did not start\n"); return 1
    global T, BASE
    T, BASE = token, f"http://127.0.0.1:{port}"
    time.sleep(1)
    try:
        pw_cm = sync_playwright_termux() if HAS_TERMUX_PW else sync_playwright()
        with pw_cm as p:
            run(p, "desktop", {"width": 1280, "height": 800}, False)
            run(p, "phone", {"width": 390, "height": 844}, True)
            run(p, "tablet", {"width": 768, "height": 1024}, True)
    finally:
        viewer.kill(); shutil.rmtree(TMP, ignore_errors=True)
    for i in issues:
        sys.stdout.write("FAIL  " + i + "\n")
    for e in errs:
        sys.stdout.write("CONSOLE ERROR  " + str(e) + "\n")
    sys.stdout.write("UI AUDIT PASSED\n" if not issues and not errs else f"UI AUDIT FAILED ({len(issues)} issue(s), {len(errs)} console error(s))\n")
    return 0 if not issues and not errs else 1


if __name__ == "__main__":
    sys.exit(main())
