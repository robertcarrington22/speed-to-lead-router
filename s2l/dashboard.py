"""The weekly readout as a live page. Plain HTML, no build step."""
import html
import time

from . import route, sla, sync

CSS = """
:root{--bg:#F7F8F6;--ink:#141A1F;--ink2:#3C4650;--ink3:#6B7680;--rule:#D6DBD8;--panel:#EEF1EE;--acc:#0E5C64;--warn:#9A5B00;--warnbg:#F6E7CB;--bad:#8B2E2E;--badbg:#F3DADA;--good:#2F6B3A;--goodbg:#DCEBDD}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:24px;margin:0 0 2px}h2{font-size:15px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);margin:30px 0 10px;font-weight:600}
.sub{color:var(--ink3);margin:0 0 18px;font-size:13.5px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--panel);border-radius:4px;padding:12px 14px}.tile .v{font-size:26px;font-variant-numeric:tabular-nums;font-weight:600}.tile .k{font-size:12.5px;color:var(--ink3)}
.tile.good .v{color:var(--good)}.tile.warn .v{color:var(--warn)}.tile.bad .v{color:var(--bad)}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);font-weight:600}td.n{font-variant-numeric:tabular-nums;font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.pill{display:inline-block;padding:1px 7px;border-radius:3px;font-size:11.5px;font-weight:600}
.pill.A{background:#D9EBEA;color:#0A474D}.pill.B{background:var(--panel);color:var(--ink2)}.pill.C{background:var(--panel);color:var(--ink3)}.pill.suppress{background:var(--badbg);color:var(--bad)}
.pill.hit{background:var(--goodbg);color:var(--good)}.pill.miss{background:var(--badbg);color:var(--bad)}.pill.pending{background:var(--warnbg);color:var(--warn)}.pill.exempt{background:var(--panel);color:var(--ink3)}
.bar{height:8px;background:var(--acc);border-radius:2px;display:inline-block;vertical-align:middle}
button{font:inherit;font-size:12.5px;padding:4px 9px;border:1px solid var(--rule);border-radius:3px;background:#fff;cursor:pointer}button:hover{border-color:var(--acc)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:28px}@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.clock{font-variant-numeric:tabular-nums;font-family:ui-monospace,Menlo,monospace}
.clock.late{color:var(--bad);font-weight:600}.clock.warn{color:var(--warn);font-weight:600}
a{color:var(--acc)}
"""


def _fmt_secs(s):
    if s is None:
        return "–"
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def _pct(x):
    return "–" if x is None else f"{x * 100:.0f}%"


def _grade_hit_rate(x):
    if x is None:
        return ""
    return "good" if x >= 0.95 else ("warn" if x >= 0.85 else "bad")


def render(m, leads, window_label):
    now = time.time()
    s = route.cfg()["sla_seconds"]
    h = m["human"]
    pending = [l for l in leads if l.get("sla_result") == "pending"]
    pending.sort(key=lambda l: l["sla_start_at"])
    recent = leads[:25]

    def esc(x):
        return html.escape(str(x if x is not None else ""))

    rows_src = "".join(
        f"<tr><td>{esc(k)}</td><td class=n>{v['n']}</td><td class=n>{v['eligible']}</td>"
        f"<td class=n>{_pct(v['hit_rate'])}</td><td class=n>{_fmt_secs(v['median'])}</td><td class=n>{_fmt_secs(v['p90'])}</td>"
        f"<td class=n>{v['meetings']}</td><td class=n>{v['funded']}</td></tr>"
        for k, v in sorted(m["by_source"].items(), key=lambda kv: -kv[1]["n"])
    )
    max_hour_n = max([v["n"] for v in m["by_hour"].values()] or [1])
    rows_hour = "".join(
        f"<tr><td class=n>{hh:02d}:00</td><td><span class=bar style='width:{int(140 * v['n'] / max_hour_n)}px'></span> <span class=n>{v['n']}</span></td>"
        f"<td class=n>{_fmt_secs(v['median'])}</td><td class=n>{_fmt_secs(v['p90'])}</td></tr>"
        for hh, v in m["by_hour"].items()
    )
    rows_miss = "".join(
        f"<tr><td>{esc(route.local(x['received_at']).strftime('%a %H:%M'))}</td><td>{esc(x['source'])}</td><td><span class='pill {esc(x['tier'])}'>{esc(x['tier'])}</span></td>"
        f"<td>{esc(x['company'])}</td><td>{esc(x['owner'])}</td><td class=n>{_fmt_secs(x['secs']) if x['secs'] else 'never'}</td><td>{esc(x['reason'])}</td></tr>"
        for x in sorted(m["miss_list"], key=lambda x: -x["received_at"])[:30]
    ) or "<tr><td colspan=7>No misses in window.</td></tr>"

    def clock_cell(l):
        el = now - l["sla_start_at"]
        if el < 0:
            return f"<span class=clock>opens in {_fmt_secs(-el)}</span>"
        cls = "late" if el > s["human_touch"] / sla.SPEED else ("warn" if el > s["escalate"] / sla.SPEED else "")
        return f"<span class='clock {cls}'>{_fmt_secs(el * sla.SPEED)}</span>"

    rows_pending = "".join(
        f"<tr><td>{clock_cell(l)}</td><td><span class='pill {esc(l['tier'])}'>{esc(l['tier'])}</span></td><td>{esc(l.get('name') or l.get('email'))}<br><small>{esc(l.get('company'))}</small></td>"
        f"<td>{esc(l['source'])}</td><td>{esc((l.get('balance_band') or '').replace('_', ' '))}</td><td>{esc(l.get('owner'))}</td>"
        f"<td>{'<a href=tel:' + esc(l['phone']) + '>call</a>' if l.get('phone') else '–'}</td>"
        f"<td><button onclick=\"touch('{l['id']}','call')\">Log call</button> <button onclick=\"touch('{l['id']}','email')\">Log email</button></td></tr>"
        for l in pending
    ) or "<tr><td colspan=8>Queue empty. Every eligible lead has a human touch.</td></tr>"

    rows_recent = "".join(
        f"<tr><td>{esc(route.local(l['received_at']).strftime('%a %H:%M'))}</td><td>{esc(l['source'])}</td><td><span class='pill {esc(l.get('tier'))}'>{esc(l.get('tier'))}</span> <span class=n>{esc(l.get('score'))}</span></td>"
        f"<td>{esc(l.get('company') or l.get('email'))}</td><td>{esc(l.get('owner'))}</td><td><span class='pill {esc(l.get('sla_result'))}'>{esc(l.get('sla_result'))}</span></td>"
        f"<td class=n>{_fmt_secs(sla.elapsed(l))}</td><td>{esc(l.get('stage_pipeline'))}</td>"
        f"<td><a href='/lead/{l['id']}'>detail</a></td></tr>"
        for l in recent
    )
    goals = {"no_contact": "a live conversation or a booking", "contacted_no_meeting": "the booking",
             "meeting_booked": "they show up", "applied_pending": "unblock KYB, then escalate to ops",
             "approved_not_funded": "the first deposit", "funded_not_active": "card, treasury, accounting sync"}
    rows_fu = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(goals.get(k, ''))}</td><td class=n>{v['active']}</td><td class=n>{v['sent']}</td><td class=n>{v['steps']}</td></tr>"
        for k, v in m.get("followup", {}).items()
    )
    # The bank's own taxonomy: Inbound Type from the HubSpot hidden field.
    by_it = {}
    for l in leads:
        k = l.get("inbound_type") or "(none)"
        d = by_it.setdefault(k, dict(n=0, secs=[], hits=0, graded=0, funded=0))
        d["n"] += 1
        if l.get("funded_at"):
            d["funded"] += 1
        if sla.human_sla_applies(l):
            if l.get("sla_result") in ("hit", "miss"):
                d["graded"] += 1
                d["hits"] += l["sla_result"] == "hit"
            if l.get("human_touch_at"):
                d["secs"].append(sla.elapsed(l))
    rows_it = "".join(
        f"<tr><td>{esc(k)}</td><td class=n>{v['n']}</td><td class=n>{_pct(v['hits'] / v['graded']) if v['graded'] else '–'}</td>"
        f"<td class=n>{_fmt_secs(sorted(v['secs'])[len(v['secs']) // 2]) if v['secs'] else '–'}</td><td class=n>{v['funded']}</td></tr>"
        for k, v in sorted(by_it.items(), key=lambda kv: -kv[1]["n"])
    )
    integ = " · ".join(f"{k}: <b>{esc(v)}</b>" for k, v in sync.status().items())
    funnel = " → ".join(f"{k} <b>{v}</b>" for k, v in [(k, m['funnel'].get(k, 0)) for k in ('lead', 'contacted', 'qualified', 'applied', 'approved', 'funded', 'active')])
    tiers = " · ".join(f"{k}: {v}" for k, v in sorted(m["by_tier"].items()))
    speed_note = f" · clock running {sla.SPEED:g}x" if sla.SPEED != 1 else ""

    return f"""<!doctype html><html><head><meta charset=utf-8><title>Speed to Lead</title><meta http-equiv=refresh content=5><style>{CSS}</style></head><body><div class=wrap>
<h1>Speed to lead</h1><p class=sub>{esc(window_label)} · SLA: human touch within {s['human_touch'] // 60} min of SLA start, escalate at {s['escalate'] // 60} min, automated touch within {s['auto_touch']}s{speed_note} · refreshes every 5s</p>
<div class=tiles>
<div class='tile {_grade_hit_rate(m['hit_rate'])}'><div class=v>{_pct(m['hit_rate'])}</div><div class=k>SLA hit rate ({m['hits']} hit / {m['misses']} miss, {m['pending']} pending)</div></div>
<div class=tile><div class=v>{_fmt_secs(h['median'])}</div><div class=k>median human first touch</div></div>
<div class=tile><div class=v>{_fmt_secs(h['p90'])}</div><div class=k>p90 human first touch</div></div>
<div class='tile {_grade_hit_rate(m['auto_within_60'])}'><div class=v>{_pct(m['auto_within_60'])}</div><div class=k>automated touch inside 60s (median {_fmt_secs(m['auto']['median'])})</div></div>
<div class=tile><div class=v>{m['total']}</div><div class=k>leads · {m['eligible']} SLA-eligible · {m['after_hours']} after hours · {m['suppressed']} suppressed</div></div>
<div class=tile><div class=v>{m['funded']}</div><div class=k>funded · ${m['funded_amount']:,.0f} · {m['meetings']} meetings booked</div></div>
</div>
<p class=sub style='margin-top:10px'>Funnel: {funnel} &nbsp;·&nbsp; Tiers: {tiers}</p>
<p class=sub>Integrations: {integ} &nbsp;·&nbsp; <a href=/reactivation>Reactivation →</a></p>

<h2>Live queue (pending human touch)</h2>
<table><tr><th>on clock</th><th>tier</th><th>who</th><th>source</th><th>balance</th><th>owner</th><th>dial</th><th>log touch</th></tr>{rows_pending}</table>

<h2>By Inbound Type (HubSpot field)</h2>
<table><tr><th>inbound type</th><th>n</th><th>hit</th><th>median</th><th>funded</th></tr>{rows_it}</table>

<div class=grid2><div>
<h2>By source</h2>
<table><tr><th>source</th><th>n</th><th>elig.</th><th>hit</th><th>median</th><th>p90</th><th>mtgs</th><th>funded</th></tr>{rows_src}</table>
</div><div>
<h2>By hour received (ET)</h2>
<table><tr><th>hour</th><th>leads</th><th>median</th><th>p90</th></tr>{rows_hour}</table>
</div></div>

<h2>Follow-up automation</h2>
<p class=sub>Sequences fire on the absence of the next event and stop the moment it happens: a reply, a booking, a stage change, a deposit.</p>
<table><tr><th>sequence</th><th>goal</th><th>leads in it now</th><th>steps sent (window)</th><th>steps</th></tr>{rows_fu}</table>

<h2>Misses, with reasons</h2>
<table><tr><th>received</th><th>source</th><th>tier</th><th>company</th><th>owner</th><th>touched at</th><th>reason</th></tr>{rows_miss}</table>

<h2>Recent leads</h2>
<table><tr><th>received</th><th>source</th><th>tier · score</th><th>company</th><th>owner</th><th>sla</th><th>touch</th><th>stage</th><th></th></tr>{rows_recent}</table>
</div>
<script>
function touch(id, ch){{fetch('/lead/'+id+'/touch',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{by:'Robert',channel:ch}})}}).then(()=>location.reload())}}
</script></body></html>"""


def reactivation_page(r):
    def esc(x):
        return html.escape(str(x if x is not None else ""))

    def money(x):
        return f"${x:,.0f}" if x else "–"

    def table(d, first_col, extra=()):
        rows = "".join(
            f"<tr><td>{esc(k)}</td>" + "".join(f"<td class=n>{esc(v.get(e, '–'))}</td>" for e in extra) +
            f"<td class=n>{v['contacted']}</td><td class=n>{v['called']}</td><td class=n>{v['replied']}</td><td class=n>{v['funded']}</td>"
            f"<td class=n>{_pct(v['funded_rate'])}</td><td class=n>{money(v['dollars'])}</td></tr>"
            for k, v in sorted(d.items(), key=lambda kv: -kv[1]["contacted"])
        ) or "<tr><td colspan=9>Nothing contacted yet.</td></tr>"
        head = "".join(f"<th>{e}</th>" for e in extra)
        return f"<table><tr><th>{first_col}</th>{head}<th>contacted</th><th>called</th><th>replied</th><th>funded ≤30d</th><th>rate</th><th>$</th></tr>{rows}</table>"

    rows_q = "".join(
        f"<tr><td><span class='pill {'A' if q['tier'] == 'call' else 'B'}'>{esc(q['tier'])}</span></td><td>{esc(q['company'])}<br><small>{esc(q['contact'])}</small></td>"
        f"<td>{esc(q['segment'].replace('_', ' '))}</td><td>{esc((q['trigger'] or '–').replace('_', ' '))}<br><small>{esc(q['trigger_detail'])}</small></td>"
        f"<td class=n>{money(q['peak'])}</td><td class=n>{q['score']}</td>"
        f"<td>{'<a href=tel:' + esc(q['phone']) + '>call</a>' if q.get('phone') else '–'}</td>"
        f"<td><small>{esc('; '.join(f'{k} {v}' for k, v in q['breakdown'].items()))}</small></td></tr>"
        for q in r["queue"][:40]
    )
    qs = r["queue_sizes"]
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Reactivation</title><style>{CSS}</style></head><body><div class=wrap>
<p class=sub><a href=/>← speed to lead</a></p>
<h1>Reactivation</h1><p class=sub>Dormant accounts, ranked by why they left, why now, and what they were worth. Funded within 30 days is the only metric that counts. Last 90 days.</p>
<div class=tiles>
<div class=tile><div class=v>{r['dormant_accounts']}</div><div class=k>dormant accounts · {r['active_accounts']} active · {money(r['dormant_peak_total'])} peak balance dormant</div></div>
<div class=tile><div class=v>{qs['call']} / {qs['email']}</div><div class=k>this week: calls / emails · {qs['hold']} on hold</div></div>
<div class=tile><div class=v>{r['contacted']}</div><div class=k>contacted in window</div></div>
<div class='tile {"good" if (r['funded_rate'] or 0) >= 0.08 else ""}'><div class=v>{_pct(r['funded_rate'])}</div><div class=k>reactivated and funded ({r['funded']})</div></div>
<div class=tile><div class=v>{money(r['dollars'])}</div><div class=k>dollars back on platform</div></div>
</div>

<h2>Does the score work?</h2>
<p class=sub>If higher scores don't fund more often, the weights are wrong. This table is the check.</p>
{table(r['by_score'], 'score band')}

<div class=grid2><div>
<h2>By segment (why they left)</h2>
{table(r['by_segment'], 'segment', ('accounts',))}
</div><div>
<h2>By trigger (why now)</h2>
{table(r['by_trigger'], 'trigger')}
</div></div>

<div class=grid2><div>
<h2>By tier</h2>
{table(r['by_tier'], 'tier')}
</div><div>
<h2>By weekly batch</h2>
{table(r['by_batch'], 'batch')}
</div></div>

<h2>This week's queue</h2>
<p class=sub>Top {qs['call']} get a call and the email. The next {qs['email']} get the email. One personalized sentence per message: the trigger.</p>
<table><tr><th>tier</th><th>account</th><th>segment</th><th>trigger</th><th>peak</th><th>score</th><th>dial</th><th>why this score</th></tr>{rows_q}</table>
</div></body></html>"""


def lead_page(lead, events):
    def esc(x):
        return html.escape(str(x if x is not None else ""))
    rows = "".join(f"<tr><td class=n>{esc(route.local(e['at']).strftime('%a %H:%M:%S'))}</td><td>{esc(e['type'])}</td><td>{esc(e['detail'])}</td></tr>" for e in events)
    bd = "".join(f"<tr><td>{esc(k)}</td><td class=n>{v}</td></tr>" for k, v in (lead.get("score_breakdown") or lead.get("breakdown") or {}).items())
    t1 = lead.get("touch_one") or {}
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Lead {esc(lead['id'])}</title><style>{CSS}</style></head><body><div class=wrap>
<p class=sub><a href=/>← dashboard</a></p>
<h1>{esc(lead.get('name') or lead.get('email'))} <span class='pill {esc(lead.get('tier'))}'>{esc(lead.get('tier'))}</span> <span class='pill {esc(lead.get('sla_result'))}'>{esc(lead.get('sla_result'))}</span></h1>
<p class=sub>{esc(lead.get('company'))} · {esc(lead['source'])} · {esc(lead.get('intent'))} · {esc(lead.get('stage'))} · {esc((lead.get('balance_band') or '').replace('_',' '))} · on {esc(lead.get('current_provider'))} · owner {esc(lead.get('owner'))} ({esc(lead.get('owner_kind'))}) · stage {esc(lead.get('stage_pipeline'))}</p>
<div class=grid2><div><h2>Score {esc(lead.get('score'))}</h2><table>{bd}</table></div>
<div><h2>Touch one</h2><p><b>{esc(t1.get('email_subject'))}</b></p><pre style='white-space:pre-wrap;font:13px/1.45 inherit;background:var(--panel);padding:12px;border-radius:4px'>{esc(t1.get('email_body'))}</pre>{('<p><b>SMS:</b> ' + esc(t1.get('sms')) + '</p>') if t1.get('sms') else ''}</div></div>
<h2>Timeline</h2><table><tr><th>at</th><th>event</th><th>detail</th></tr>{rows}</table>
</div></body></html>"""
