"""Render examples/dashboard.yaml to PNG screenshots with invented sample data.

    python examples/render_screenshots.py

The real button-card (downloaded once into examples/.cache/) draws every
custom:button-card from the dashboard file itself, fed with the made-up
readings below — no real plate, place or account — inside a minimal page in
Home Assistant's dark style. Headless Edge or Chrome captures the pages into
examples/images/. Other card types are drawn as simple look-alikes, and the
notifications page mirrors the texts the integration sends.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
OUT = HERE / "images"
CACHE = HERE / ".cache"
BUTTON_CARD = (
    "https://github.com/custom-cards/button-card/releases/download/v7.0.1/button-card.js"
)
CAR = "12345678"
TZ = timezone(timedelta(hours=3))
NOW = datetime(2026, 5, 18, 10, 24, tzinfo=TZ)
ZONES = ["—", "חיפה – רחבי העיר [5]", "CITY – ZONE [111]", "CITY – ZONE [222]"]


def st(state, **attrs):
    return {"state": str(state), "attributes": attrs}


def month_rows() -> list[dict]:
    rows = []
    for i, (day, start, end, price) in enumerate(
        [(16, "08:10", "10:40", 14.2), (14, "17:05", "18:00", 5.5), (11, "09:00", "12:00", 20.4),
         (7, "13:20", "14:05", 4.3)]
    ):
        rows.append({"car": CAR, "where": "חיפה, רחבי העיר", "price": price,
                     "started": f"{day:02d}-05-2026 {start}:00", "ended": f"{day:02d}-05-2026 {end}:00"})
    return rows


def states(parked: bool) -> dict[str, dict]:
    started = NOW - timedelta(minutes=38)
    ends = NOW + timedelta(minutes=82)
    s = {
        f"binary_sensor.cello_{CAR}_parking": st(
            "on" if parked else "off", zone_id=5 if parked else None, zone="רחבי העיר"
        ),
        f"sensor.cello_{CAR}_time_left": st(82 if parked else "unknown", total_minutes=120),
        f"binary_sensor.cello_{CAR}_moved_while_parked": st("off", distance_from_parking_m=12 if parked else None),
        f"sensor.cello_{CAR}_location": st("חיפה", distance_from_parking_m=12 if parked else None),
        f"select.cello_{CAR}_parking_zone": st(ZONES[1], options=ZONES),
        f"select.cello_{CAR}_suggested_zone": st(ZONES[1], options=["…", ZONES[1]]),
        f"number.cello_{CAR}_park_for": st(2),
        f"sensor.cello_{CAR}_parking_started": st(started.isoformat() if parked else "unknown"),
        f"sensor.cello_{CAR}_parking_ends": st(ends.isoformat() if parked else "unknown"),
        f"sensor.cello_{CAR}_parking_cost": st("4.60" if parked else "unknown"),
        f"sensor.cello_{CAR}_spent_this_month": st("44.40"),
        f"switch.cello_{CAR}_auto_renew": st("on"),
        f"text.cello_{CAR}_zone_search": st(""),
        "sensor.cello_spent_this_month": st(
            "44.40", last_parkings=month_rows(), report_pdf="#", parkings=4
        ),
        "sensor.cello_parkings_this_month": st(4),
        "sensor.cello_last_invoice": st("61.20", url="#"),
        "sensor.cello_payment_card": st("1234"),
        "switch.cello_cello_auto_extend": st("off"),
        "switch.cello_cello_auto_stop": st("off"),
    }
    return {k: {"entity_id": k, **v, "last_changed": NOW.isoformat()} for k, v in s.items()}


HEAD = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css">
<style>
:root{--primary-text-color:#e1e1e1;--secondary-text-color:#9b9b9b;--secondary-background-color:#2b2b2b;
--card-background-color:#1c1c1c;--ha-card-background:#1c1c1c;--divider-color:rgba(225,225,225,.12);
--paper-item-icon-color:#44739e;--primary-color:#03a9f4;--state-icon-color:#44739e}
html,body{overflow:hidden}
body{margin:0;background:#111;font-family:Roboto,Arial,sans-serif;color:#e1e1e1}
main{width:440px;margin:0 auto;padding:14px 0}
.g{display:grid;gap:8px;margin-bottom:8px}
.stub{background:#1c1c1c;border-radius:22px;padding:6px 16px;margin-bottom:8px;font-size:14px;direction:rtl}
.row{padding:10px 0;display:flex;justify-content:space-between;align-items:center}
.h{font-weight:700;font-size:16px;margin:16px 4px 8px;direction:rtl}
.sub{font-weight:500;font-size:13px;margin:12px 4px 6px;color:#ccc;direction:rtl}
.tog{display:inline-block;width:34px;height:14px;border-radius:7px;background:#555;position:relative}
.tog.on{background:#03a9f480}.tog i{position:absolute;top:-3px;width:20px;height:20px;border-radius:50%;background:#bbb;left:-2px}
.tog.on i{background:#03a9f4;left:16px}
.bars{display:flex;align-items:flex-end;gap:10px;height:110px;padding:8px 4px}
.bars div{flex:1;background:#29c5c8;border-radius:6px 6px 0 0}
</style>
<script>
const MDI = 'https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css';
customElements.define('ha-card', class extends HTMLElement { connectedCallback(){ this.style.display='block'; this.style.overflow='hidden'; } });
class MdiIcon extends HTMLElement {
  set icon(v) { this._icon = v; this.draw(); } get icon() { return this._icon; }
  set stateObj(v) { this._state = v; this.draw(); }
  connectedCallback() { this.draw(); }
  draw() {
    const n = (this._icon || this.getAttribute('icon') || (this._state && this._state.attributes.icon) || 'mdi:parking').replace('mdi:', '');
    const size = this.style.getPropertyValue('--mdc-icon-size') || '24px';
    this.style.display = 'inline-flex';
    // Inside button-card's shadow DOM the page's MDI classes do not apply, so
    // each icon carries its own copy of the stylesheet.
    const root = this.shadowRoot || this.attachShadow({ mode: 'open' });
    root.innerHTML = '<link rel="stylesheet" href="' + MDI + '"><i class="mdi mdi-' + n + '" style="font-size:' + size + ';line-height:1"></i>';
  }
}
customElements.define('ha-icon', class extends MdiIcon {});
customElements.define('ha-state-icon', class extends MdiIcon {});
customElements.define('ha-ripple', class extends HTMLElement {});
customElements.define('mwc-ripple', class extends HTMLElement {});
window.loadCardHelpers = async () => ({});
</script></head><body><main id="m"></main>
"""

BODY = """<script src="__BC__"></script>
<script>
(() => {
const STATES = __STATES__;
const SECTIONS = __SECTIONS__;
const hass = { states: STATES, user: { name: 'User', is_admin: true }, language: 'he',
  locale: { language: 'he', number_format: 'language', time_format: '24' },
  themes: { darkMode: true, themes: {} }, config: { time_zone: 'Asia/Jerusalem', unit_system: {} },
  entities: {}, devices: {}, areas: {}, localize: k => k, formatEntityState: s => s.state,
  formatEntityAttributeValue: (s, a) => s.attributes[a], callService: () => {} };
const val = e => { const s = STATES[e]; if (!s) return '';
  if (e.startsWith('switch.')) return '<span class="tog ' + (s.state === 'on' ? 'on' : '') + '"><i></i></span>';
  if (e.startsWith('text.')) return '<span style="color:#777">—</span>';
  if (e.startsWith('select.')) return s.state.replace(/\\s*\\[\\d+\\]$/, '');
  return (e.includes('spent') ? '₪' : '') + s.state; };
function card(c) {
  if (c.type === 'custom:button-card') { const el = document.createElement('button-card'); el.setConfig(c); el.hass = hass; return el; }
  const d = document.createElement('div');
  if (c.type === 'heading') { d.className = c.heading_style ? 'sub' : 'h'; d.textContent = c.heading; return d; }
  d.className = 'stub';
  if (c.type === 'entities') d.innerHTML = c.entities.map(e => '<div class="row"><span>' + e.name + '</span><span style="color:#9b9b9b">' + val(e.entity) + '</span></div>').join('');
  else if (c.type === 'statistics-graph') d.innerHTML = '<div class="row"><b>' + (c.title || '') + '</b></div><div class="bars">' + [38, 52, 61, 47, 70, 44].map(h => '<div style="height:' + h + '%"></div>').join('') + '</div>';
  else d.textContent = '[' + c.type + ']';
  return d;
}
const root = document.getElementById('m');
customElements.whenDefined('button-card').then(() => {
  for (const sec of SECTIONS) {
    const s = document.createElement('section');
    for (const c of sec) {
      if (c.grid) { const g = document.createElement('div'); g.className = 'g'; g.style.gridTemplateColumns = 'repeat(' + c.grid + ',1fr)'; c.cards.forEach(x => g.appendChild(card(x))); s.appendChild(g); }
      else { const el = card(c); el.style.display = 'block'; el.style.marginBottom = '8px'; s.appendChild(el); }
    }
    root.appendChild(s);
  }
});
})();
</script></body></html>
"""


NOTIFY_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>
html,body{overflow:hidden}
body{margin:0;background:#111;font-family:Roboto,-apple-system,Arial,sans-serif;color:#e1e1e1}
main{width:440px;margin:0 auto;padding:14px 0}
.t{font-size:13px;color:#9b9b9b;margin:18px 6px 8px;text-transform:uppercase;letter-spacing:.06em}
.n{background:#1c1c1c;border:1px solid #2e2e2e;border-radius:14px;margin-bottom:12px}
.n h3{margin:0;padding:16px 18px 6px;font-size:20px;font-weight:400}
.n p{margin:0;padding:4px 18px 0;font-size:14px;line-height:1.45;white-space:pre-line}
.n .ago{text-align:right;color:#9b9b9b;font-size:13px;padding:8px 18px 12px}
.n .act{border-top:1px solid #2e2e2e;text-align:right;padding:12px 22px;color:#29b6f6;font-weight:500}
.push{background:rgba(60,60,64,.92);border-radius:22px;padding:12px 14px;color:#fff;font-family:-apple-system,Arial,sans-serif}
.push .top{display:flex;align-items:center;gap:8px;font-size:12px;color:#c8c8cc}
.push .app{width:20px;height:20px;border-radius:5px;background:#18bcf2;display:inline-block}
.push b{display:block;font-size:15px;margin-top:6px}
.push span{font-size:14px;line-height:1.35;display:block;margin-top:2px}
.btns{margin-top:8px;background:rgba(60,60,64,.92);border-radius:14px;overflow:hidden}
.btns div{padding:12px 16px;font-size:16px;border-top:1px solid rgba(255,255,255,.08)}
.btns div:first-child{border-top:none;color:#ff453a}
</style></head><body><main>
<div class="t">Phone push (long-press for actions)</div>
<div class="push"><div class="top"><i class="app"></i>HOME ASSISTANT<span style="margin-left:auto">now</span></div>
<b>🅿️ Parking still running</b>
<span>Car 12345678 seems to be moved (850 m away) while Cello parking in רחבי העיר is still running.</span></div>
<div class="btns"><div>Stop parking</div><div>Keep it running</div></div>
<div class="t">Home Assistant notifications</div>
<div class="n"><h3>Cello: parking still running</h3>
<p>Car 12345678 seems to be moved (850 m away) while Cello parking in רחבי העיר is still running.</p>
<div class="ago">26 seconds ago</div><div class="act">Dismiss</div></div>
<div class="n"><h3>Cello parking still running</h3>
<p>Car 12345678 has been parked with Cello for 120 minutes (רחבי העיר).</p>
<div class="ago">3 minutes ago</div><div class="act">Dismiss</div></div>
<div class="n"><h3>Cello: confirm parking for car 12345678</h3>
<p>Parking is free at this hour in this zone. Start anyway?

Press Start parking again within 3 minutes to accept.</p>
<div class="ago">1 hour ago</div><div class="act">Dismiss</div></div>
</main></body></html>
"""


def flatten(cards: list[dict], st_now: dict) -> list[dict]:
    """Sections' cards as the page draws them: grids as rows, conditionals resolved."""
    out: list[dict] = []
    for c in cards:
        t = c.get("type")
        if t == "conditional":
            ok = all(
                (st_now.get(cond["entity"]) or {}).get("state") == cond["state"]
                for cond in c.get("conditions", [])
            )
            if ok:
                out.extend(flatten([c["card"]], st_now))
        elif t == "vertical-stack":
            out.extend(flatten(c["cards"], st_now))
        elif t == "grid" and c.get("columns"):
            out.append({"grid": c["columns"], "cards": flatten(c["cards"], st_now)})
        elif t == "grid":
            out.extend(flatten(c["cards"], st_now))
        else:
            out.append(c)
    return out


def browser() -> str:
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
    ):
        if p and Path(p).exists():
            return p
    sys.exit("Needs Edge or Chrome for headless screenshots")


def main() -> None:
    CACHE.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    bc = CACHE / "button-card.js"
    if not bc.exists():
        urllib.request.urlretrieve(BUTTON_CARD, bc)
    dash = yaml.safe_load((HERE / "dashboard.yaml").read_text(encoding="utf-8"))
    sections = dash["views"][0]["sections"]
    shots = {
        "dashboard-parked.png": (True, [0], 470),
        "dashboard-not-parked.png": (False, [0], 720),
        "dashboard-account.png": (False, [1, 2], 920),
    }
    exe = browser()
    with tempfile.TemporaryDirectory() as tmp:
        for name, (parked, wanted, height) in shots.items():
            s = states(parked)
            page = HEAD + BODY.replace("__BC__", bc.as_uri()).replace(
                "__STATES__", json.dumps(s, ensure_ascii=False)
            ).replace(
                "__SECTIONS__",
                json.dumps([flatten(sections[i]["cards"], s) for i in wanted], ensure_ascii=False),
            )
            html_path = Path(tmp) / (name + ".html")
            html_path.write_text(page, encoding="utf-8")
            target = OUT / name
            subprocess.run(
                [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                 "--force-device-scale-factor=2", f"--window-size=500,{height}",
                 "--virtual-time-budget=6000", "--allow-file-access-from-files",
                 f"--screenshot={target}", html_path.as_uri()],
                check=True, capture_output=True, timeout=120,
            )
            print("wrote", target)
        html_path = Path(tmp) / "notifications.html"
        html_path.write_text(NOTIFY_PAGE, encoding="utf-8")
        target = OUT / "notifications.png"
        subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=2", "--window-size=500,900",
             f"--screenshot={target}", html_path.as_uri()],
            check=True, capture_output=True, timeout=120,
        )
        print("wrote", target)


if __name__ == "__main__":
    main()
