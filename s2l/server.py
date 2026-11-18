"""HTTP surface. One webhook in, a dashboard and a few JSON endpoints out.

POST /webhook/<source>          inbound lead payload (JSON)
POST /lead/<id>/touch           {"by": "Robert", "channel": "call|email|sms"}   human first touch
POST /lead/<id>/stage           {"stage": "qualified|applied|approved|funded|active", "amount": 50000}
POST /lead/<id>/meeting         {"at": epoch}   meeting booked (defaults to now)
GET  /                          dashboard (last 7 days)
GET  /lead/<id>                 lead detail page
GET  /api/metrics               JSON metrics (last 7 days)
GET  /api/leads                 JSON leads (last 7 days)
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import xml.etree.ElementTree as ET

from . import dashboard, followup, pipeline, reactivation, sla, store, sync

WINDOW_SECONDS = 7 * 86400

SFDC_ACK = ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>'
            '<notificationsResponse xmlns="http://soap.sforce.com/2005/09/outbound"><Ack>true</Ack>'
            '</notificationsResponse></soapenv:Body></soapenv:Envelope>')


def parse_sfdc_outbound_message(xml_bytes):
    """Salesforce Outbound Message (SOAP). Returns a list of flat {FieldName: value} dicts, one per notification."""
    root = ET.fromstring(xml_bytes)
    out = []
    for sobj in root.iter():
        if sobj.tag.split("}")[-1] != "sObject":
            continue
        rec = {}
        for child in sobj:
            rec[child.tag.split("}")[-1]] = (child.text or "").strip() or None
        if rec:
            out.append(rec)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console; alerts matter more than access logs
        if self.path.startswith("/webhook") or self.path.startswith("/lead"):
            super().log_message(fmt, *args)

    def _send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # browser refreshed mid-response; nothing to do

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _html(self, code, text):
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw else {}

    def do_GET(self):
        path = self.path.split("?")[0]
        since = time.time() - WINDOW_SECONDS
        if path == "/":
            leads = store.list_leads(since=since)
            return self._html(200, dashboard.render(sla.metrics(leads), leads, "Last 7 days"))
        if path == "/api/metrics":
            return self._json(200, sla.metrics(store.list_leads(since=since)))
        if path == "/api/config":
            from . import route
            return self._json(200, {"speed": sla.SPEED, "sla_seconds": route.cfg()["sla_seconds"],
                                    "business_hours": route.cfg()["business_hours"], "integrations": sync.status()})
        if path == "/api/integrations":
            return self._json(200, sync.status())
        if path == "/reactivation":
            return self._html(200, dashboard.reactivation_page(reactivation.report()))
        if path == "/api/reactivation":
            return self._json(200, reactivation.report())
        if path == "/api/leads":
            return self._json(200, store.list_leads(since=since))
        if path.startswith("/lead/"):
            lid = path.split("/")[2]
            lead = store.get_lead(lid)
            if not lead:
                return self._json(404, {"error": "not found"})
            if path.endswith("/json"):
                return self._json(200, {"lead": lead, "events": store.list_events(lid)})
            return self._html(200, dashboard.lead_page(lead, store.list_events(lid)))
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        ctype = (self.headers.get("Content-Type") or "").lower()

        # Salesforce Outbound Messages arrive as SOAP XML and must be acknowledged with XML.
        if path == "/webhook/salesforce_lead" and "xml" in ctype:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                records = parse_sfdc_outbound_message(raw)
            except ET.ParseError as e:
                return self._send(400, f"bad xml: {e}".encode(), "text/plain")
            for rec in records:
                pipeline.process("salesforce_lead", rec)
            return self._send(200, SFDC_ACK.encode(), "text/xml; charset=utf-8")

        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._json(400, {"error": "body must be JSON"})

        if path == "/reactivation/import":
            n = reactivation.import_accounts(body if isinstance(body, list) else body.get("accounts", []))
            return self._json(200, {"imported": n})
        if path == "/reactivation/run":
            return self._json(200, reactivation.run_week(batch_label=body.get("label")))
        if path.startswith("/account/") and path.endswith("/funded"):
            aid = path.split("/")[2]
            res = reactivation.record_outcome(aid, float(body.get("amount") or 0))
            return self._json(200 if res else 404, res or {"error": "unknown account"})

        if path.startswith("/webhook/"):
            source = path.split("/")[2]
            # HubSpot workflow webhooks and Intercom can batch several events in one POST.
            items = body if isinstance(body, list) else [body]
            results = []
            for item in items:
                try:
                    lead = pipeline.process(source, item)
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                results.append({"id": lead["id"], "tier": lead.get("tier"), "score": lead.get("score"),
                                "owner": lead.get("owner"), "owner_kind": lead.get("owner_kind"),
                                "inbound_type": lead.get("inbound_type"), "lead_source": lead.get("lead_source"),
                                "after_hours": lead.get("after_hours"), "suppressed": lead.get("suppressed")})
            return self._json(201, results[0] if len(results) == 1 else results)
        if path.startswith("/lead/"):
            parts = path.split("/")
            lid, action = parts[2], parts[3] if len(parts) > 3 else ""
            if not store.get_lead(lid):
                return self._json(404, {"error": "not found"})
            if action == "touch":
                lead = sla.record_human_touch(lid, body.get("by", "rep"), body.get("channel", "call"),
                                              reached=body.get("reached", True))
                return self._json(200, {"id": lid, "sla_result": lead.get("sla_result"),
                                        "seconds": sla.elapsed(lead), "contact_reached": lead.get("contact_reached")})
            if action == "reply":
                store.add_event(lid, "replied", body.get("text", "")[:200])
                return self._json(200, store.update_lead(lid, replied_at=time.time()))
            if action == "optout":
                store.add_event(lid, "opted_out", "")
                return self._json(200, store.update_lead(lid, opted_out=True))
            if action == "stage":
                st = body.get("stage", "qualified")
                lead = store.get_lead(lid)
                fields = {"stage_pipeline": st, "stage_times": dict(lead.get("stage_times") or {}, **{st: time.time()})}
                if st == "funded":
                    fields["funded_at"] = time.time()
                    fields["funded_amount"] = float(body.get("amount") or 0)
                store.add_event(lid, "stage", st)
                updated = store.update_lead(lid, **fields)
                sync.on_stage(updated)
                return self._json(200, updated)
            if action == "meeting":
                store.add_event(lid, "meeting_booked", "")
                updated = store.update_lead(lid, meeting_at=float(body.get("at") or time.time()), stage_pipeline="qualified")
                sync.on_meeting(updated)
                return self._json(200, updated)
        return self._json(404, {"error": "not found"})


def _followup_loop():
    """Every 15 seconds (scaled), fire any follow-up steps that have come due."""
    while True:
        try:
            n = followup.run_once(speed=sla.SPEED)
            if n:
                print(f"[followup] fired {n} step(s)", flush=True)
        except Exception as e:  # keep the loop alive no matter what
            print(f"[followup] error: {e}", flush=True)
        time.sleep(max(1.0, 15.0 / sla.SPEED))


def serve(host="127.0.0.1", port=8765):
    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=_followup_loop, daemon=True).start()
    print(f"speed-to-lead listening on http://{host}:{port}  (clock {sla.SPEED:g}x)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
