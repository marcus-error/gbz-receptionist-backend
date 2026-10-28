try:
    import pyVoIP
    import pyVoIP.SIP
    import pyVoIP.VoIP

    if hasattr(pyVoIP, "REGISTER_FAILURE_THRESHOLD"):
        pyVoIP.REGISTER_FAILURE_THRESHOLD = 999999

    for method in ["OPTIONS", "REGISTER", "INFO", "NOTIFY", "SUBSCRIBE", "UPDATE", "MESSAGE", "PRACK"]:
        if hasattr(pyVoIP, "SIPCompatibleMethods") and method not in pyVoIP.SIPCompatibleMethods:
            pyVoIP.SIPCompatibleMethods.append(method)

    if hasattr(pyVoIP.VoIP, "VoIPPhone"):
        _orig_voip_callback = pyVoIP.VoIP.VoIPPhone.callback
        
        def _patched_voip_callback(self, request):
            if getattr(request, "method", None) == "OPTIONS":
                try:
                    import logging
                    logger = logging.getLogger(__name__)
                    cid = getattr(request.headers, "Call-ID", "")
                    if isinstance(cid, dict):
                        cid = cid.get("raw", str(cid))
                    logger.info(f"[SIP OPTIONS] Received OPTIONS ping in VoIPPhone.callback! Replying with 200 OK (Call-ID: {cid})")
                    ok_resp = self.sip.gen_ok(request)
                    self.sip.out.sendto(ok_resp.encode("utf8"), (self.sip.server, self.sip.port))
                except Exception as e:
                    pass
                return
            return _orig_voip_callback(self, request)

        pyVoIP.VoIP.VoIPPhone.callback = _patched_voip_callback

        # Patch _gen_response_via_header to ensure Asterisk RFC 3581 compliance
        _orig_gen_via = pyVoIP.SIP.SIPClient._gen_response_via_header
        def _patched_gen_via(self_sip, request):
            via = ""
            for h_via in request.headers.get("Via", []):
                v_line = (
                    "Via: SIP/2.0/UDP "
                    + f"{h_via['address'][0]}:{h_via['address'][1]}"
                )
                if "branch" in h_via.keys():
                    v_line += f";branch={h_via['branch']}"
                # Asterisk RFC 3581 compliance: provide rport and received
                if "rport" in h_via.keys() and h_via["rport"] is not None:
                    v_line += f";rport={h_via['rport']}"
                else:
                    v_line += f";rport={self_sip.port};received={self_sip.server}"
                if "received" in h_via.keys() and ";received=" not in v_line:
                    v_line += f";received={h_via['received']}"
                v_line += "\r\n"
                via += v_line
            return via
        pyVoIP.SIP.SIPClient._gen_response_via_header = _patched_gen_via

        _orig_gen_ok = pyVoIP.SIP.SIPClient.gen_ok
        def _patched_gen_ok(self_sip, request):
            resp = _orig_gen_ok(self_sip, request)
            
            # Asterisk requires the Contact header
            if "Contact:" not in resp:
                contact_hdr = f"Contact: <sip:{self_sip.username}@{self_sip.myIP}:{self_sip.myPort}>\r\n"
                resp = resp.replace("Content-Length:", f"{contact_hdr}Content-Length:")
                
            # Asterisk requires Via header to contain rport and received
            import re
            via_match = re.search(r"Via:\s*(.*?)\r?\n", resp, re.IGNORECASE)
            if via_match:
                via = via_match.group(1)
                if ";rport" in via and ";rport=" not in via:
                    new_via = via.replace(";rport", f";rport={self_sip.port};received={self_sip.server}")
                    resp = resp.replace(f"Via: {via}", f"Via: {new_via}")
                elif ";rport=" not in via:
                    new_via = via + f";rport={self_sip.port};received={self_sip.server}"
                    resp = resp.replace(f"Via: {via}", f"Via: {new_via}")
            
            return resp
        pyVoIP.SIP.SIPClient.gen_ok = _patched_gen_ok

        # Patch both parse_message and parseMessage
        _orig_parse_message = pyVoIP.SIP.SIPClient.parse_message
        def _patched_parse_message(self_sip, message):
            try:
                raw_str = message.raw.decode('utf-8', errors='ignore')
                import logging
                logger = logging.getLogger(__name__)

                # Intercept OPTIONS pings immediately and reply with 200 OK to keep Asterisk happy
                if getattr(message, "method", None) == "OPTIONS" or raw_str.strip().startswith("OPTIONS"):
                    try:
                        cid = getattr(message.headers, "Call-ID", "")
                        if isinstance(cid, dict):
                            cid = cid.get("raw", str(cid))
                        logger.info(f"[SIP OPTIONS] Intercepted OPTIONS ping in parse_message! Crafting raw 200 OK (Call-ID: {cid})")
                        ok_resp = self_sip.gen_ok(message)
                        self_sip.out.sendto(ok_resp.encode("utf8"), (self_sip.server, self_sip.port))
                    except Exception as ex:
                        logger.error(f"[SIP OPTIONS] Failed to reply: {ex}")
                    return

                status = getattr(message.status, "code", 0) if getattr(message, "status", None) else 0
                cseq = getattr(message.headers, "CSeq", "")
                if isinstance(cseq, dict):
                    cseq = str(cseq.get("method", ""))
                
                # Check if this is a 200 OK for our REGISTER
                if status == 200 and "REGISTER" in str(cseq):
                    via = getattr(message.headers, "Via", "")
                    if isinstance(via, list) and via:
                        via_entry = via[0]
                        actual_port = via_entry.get("rport")
                        if actual_port and int(actual_port) != self_sip.myPort:
                            actual_port = int(actual_port)
                            logger.info(f"[SIP NAT] NAT Symmetric Port Discovery! Updating myPort from {self_sip.myPort} to {actual_port}")
                            self_sip.myPort = actual_port
                            import threading
                            def re_reg():
                                import time
                                time.sleep(1)
                                try:
                                    if getattr(self_sip, "NSD", False):
                                        self_sip.register()
                                        logger.info("[SIP NAT] Re-registration successful with correct NAT port.")
                                except Exception as e:
                                    logger.error(f"[SIP NAT] Re-registration failed: {e}")
                            threading.Thread(target=re_reg, daemon=True).start()
                            
            except Exception as e:
                pass
            return _orig_parse_message(self_sip, message)
        pyVoIP.SIP.SIPClient.parse_message = _patched_parse_message
        pyVoIP.SIP.SIPClient.parseMessage = _patched_parse_message

        # 5. Patch gen_ack to generate strictly RFC 3261 compliant ACKs for 200 OKs
        _orig_gen_ack = pyVoIP.SIP.SIPClient.gen_ack
        def _patched_gen_ack(self_sip, request):
            import uuid
            import re
            import logging
            logger = logging.getLogger(__name__)

            # RFC 3261 Section 13.2.2.4: Request-URI MUST be the URI from Contact header of 200 OK
            raw_contact = request.headers.get("Contact") or request.headers.get("contact")
            if isinstance(raw_contact, dict):
                raw_contact = raw_contact.get("raw", "")
            raw_contact = str(raw_contact or "").strip()

            target_uri = None
            if raw_contact:
                m = re.search(r"<([^>]+)>", raw_contact)
                target_uri = m.group(1).strip() if m else raw_contact.split(";")[0].strip()

            if not target_uri:
                to_hdr = request.headers.get("To", {})
                raw_to = to_hdr.get("raw", str(to_hdr)) if isinstance(to_hdr, dict) else str(to_hdr)
                m = re.search(r"<([^>]+)>", str(raw_to))
                target_uri = m.group(1).strip() if m else str(raw_to).strip()

            # Clean target_uri: strip parameters and ensure NO duplicate 'sip:' prefix
            target_uri = target_uri.split(";")[0].strip()
            if target_uri.lower().startswith("sip:"):
                target_uri = target_uri[4:]
            elif target_uri.lower().startswith("sips:"):
                target_uri = target_uri[5:]
            target_uri = target_uri.strip()

            ackMessage = f"ACK sip:{target_uri} SIP/2.0\r\n"

            # Via MUST have a new branch for 200 OK ACK (RFC 3261 Section 13.2.2.4)
            branch = f"z9hG4bK-ack-{uuid.uuid4().hex[:12]}"
            ackMessage += f"Via: SIP/2.0/UDP {self_sip.myIP}:{self_sip.myPort};branch={branch}\r\n"
            ackMessage += "Max-Forwards: 70\r\n"

            # Route header from Record-Route in reverse order if present
            route_hdrs = request.headers.get("Record-Route") or request.headers.get("record-route")
            if route_hdrs:
                if isinstance(route_hdrs, list):
                    for r in reversed(route_hdrs):
                        r_val = r.get("raw", str(r)) if isinstance(r, dict) else str(r)
                        ackMessage += f"Route: {r_val}\r\n"
                elif isinstance(route_hdrs, dict):
                    ackMessage += f"Route: {route_hdrs.get('raw', str(route_hdrs))}\r\n"
                else:
                    ackMessage += f"Route: {str(route_hdrs)}\r\n"

            # To header MUST exactly match the To header in 200 OK including the tag
            to_val = request.headers.get("To", {})
            if isinstance(to_val, dict):
                to_raw = to_val.get("raw", "")
                tag = to_val.get("tag")
                if tag and ";tag=" not in to_raw.lower():
                    to_raw = f"{to_raw};tag={tag}"
            else:
                to_raw = str(to_val)
            ackMessage += f"To: {to_raw}\r\n"

            # From header MUST exactly match From header in 200 OK
            from_val = request.headers.get("From", {})
            if isinstance(from_val, dict):
                from_raw = from_val.get("raw", "")
                tag = from_val.get("tag")
                if tag and ";tag=" not in from_raw.lower():
                    from_raw = f"{from_raw};tag={tag}"
            else:
                from_raw = str(from_val)
            ackMessage += f"From: {from_raw}\r\n"

            call_id = request.headers.get("Call-ID", "")
            if isinstance(call_id, dict):
                call_id = call_id.get("raw", str(call_id))
            ackMessage += f"Call-ID: {call_id}\r\n"

            cseq = request.headers.get("CSeq", {})
            cseq_num = cseq.get("check", "1") if isinstance(cseq, dict) else str(cseq).split()[0]
            ackMessage += f"CSeq: {cseq_num} ACK\r\n"
            ackMessage += f"User-Agent: pyVoIP {pyVoIP.__version__}\r\n"
            ackMessage += "Content-Length: 0\r\n\r\n"

            logger.info(f"[SIP OUTBOUND ACK GENERATED] Call-ID={call_id} Target=sip:{target_uri} CSeq={cseq_num} ACK")
            return ackMessage

        pyVoIP.SIP.SIPClient.gen_ack = _patched_gen_ack
        pyVoIP.SIP.SIPClient.genAck = _patched_gen_ack

        # 6. Patch VoIPPhone._callback_RESP_OK to reliably deliver ACK on initial answer AND retransmissions
        _orig_resp_ok = pyVoIP.VoIP.VoIPPhone._callback_RESP_OK
        def _patched_resp_ok(self_phone, request):
            import logging
            logger = logging.getLogger(__name__)
            call_id = request.headers.get("Call-ID", "")
            if isinstance(call_id, dict):
                call_id = call_id.get("raw", str(call_id))
            call_id = str(call_id).strip()

            call = self_phone.calls.get(call_id)
            if not call:
                for k, c in self_phone.calls.items():
                    if k in call_id or call_id in k:
                        call = c
                        break

            if not call:
                logger.warning(f"[SIP 200 OK] Received 200 OK for unknown Call-ID: {call_id}")
                return

            from pyVoIP.VoIP import CallState
            if call.state == CallState.DIALING:
                logger.info(f"[SIP 200 OK] Outbound call ANSWERED by remote: {call_id}")
                call.answered(request)
            else:
                logger.info(f"[SIP 200 OK RETRANSMIT] Remote retransmitted 200 OK for: {call_id}")

            ack = self_phone.sip.gen_ack(request)

            import re
            contact_raw = request.headers.get("Contact") or request.headers.get("contact")
            contact_str = contact_raw.get("raw", str(contact_raw)) if isinstance(contact_raw, dict) else str(contact_raw or "")
            m = re.search(r'<sip:(?:.*?@)?([^:>]+)(?::(\d+))?', contact_str)

            destinations = [(self_phone.server, self_phone.port)]
            if m:
                c_host = m.group(1).strip()
                c_port = int(m.group(2)) if m.group(2) else 5060
                if (c_host, c_port) not in destinations:
                    destinations.append((c_host, c_port))

            for dest_host, dest_port in destinations:
                try:
                    self_phone.sip.out.sendto(ack.encode("utf8"), (dest_host, dest_port))
                    logger.info(f"[SIP ACK SENT] Call-ID={call_id} -> {dest_host}:{dest_port}")
                except Exception as send_err:
                    logger.warning(f"[SIP ACK SEND ERROR] Failed sending ACK to {dest_host}:{dest_port}: {send_err}")

        pyVoIP.VoIP.VoIPPhone._callback_RESP_OK = _patched_resp_ok

        # 7. Patch VoIPPhone._callback_MSG_Bye for clean BYE handling and logging
        _orig_msg_bye = pyVoIP.VoIP.VoIPPhone._callback_MSG_Bye
        def _patched_msg_bye(self_phone, request):
            import logging
            logger = logging.getLogger(__name__)
            call_id = request.headers.get("Call-ID", "")
            if isinstance(call_id, dict):
                call_id = call_id.get("raw", str(call_id))
            logger.info(f"[SIP BYE RECEIVED] Remote hangup signal received for Call-ID: {call_id}")
            _orig_msg_bye(self_phone, request)

        pyVoIP.VoIP.VoIPPhone._callback_MSG_Bye = _patched_msg_bye

        # Fix RTP Jitter and Codec Distortion
        # 0. Patch RTPClient start to use a unified socket (Symmetric RTP)
        _orig_rtp_start = pyVoIP.RTP.RTPClient.start
        def _patched_rtp_start(self):
            import socket
            from threading import Timer
            self.sin = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.sin.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except Exception:
                pass
            self.sout = self.sin # SYMMETRIC RTP: Send and receive on the SAME port!
            
            try:
                self.sin.bind(('0.0.0.0', self.inPort))
            except OSError as e:
                import logging
                logging.getLogger(__name__).error(f"[RTP BIND] Failed to bind to 0.0.0.0:{self.inPort} - {e}")
                
            self.sin.setblocking(False)
            
            r = Timer(0, self.recv)
            r.name = "RTP Receiver"
            r.start()
            
            t = Timer(0, self.trans)
            t.name = "RTP Transmitter"
            t.start()
            
        pyVoIP.RTP.RTPClient.start = _patched_rtp_start

        # 1. Patch RTPPacketManager to pad with standard G.711 PCMU silence bytes (0xFF)
        _orig_read = pyVoIP.RTP.RTPPacketManager.read
        def _patched_pm_read(self_pm, length=160):
            import time
            while self_pm.rebuilding:
                time.sleep(0.01)
            self_pm.bufferLock.acquire()
            packet = self_pm.buffer.read(length)
            if len(packet) < length:
                pad_byte = b"\xff"  # Standard G.711 PCMU (mu-law) silence byte (0xFF = 0 amplitude)
                packet = packet + (pad_byte * (length - len(packet)))
            self_pm.bufferLock.release()
            return packet
        pyVoIP.RTP.RTPPacketManager.read = _patched_pm_read

        def _patched_pm_clear(self_pm):
            import io
            try:
                self_pm.bufferLock.acquire()
                self_pm.buffer = io.BytesIO()
                self_pm.log = {}
                self_pm.offset = 4294967296
            finally:
                self_pm.bufferLock.release()
        pyVoIP.RTP.RTPPacketManager.clear = _patched_pm_clear

        def _patched_rtp_clear_tx(self_rtp):
            if hasattr(self_rtp, "pmout") and self_rtp.pmout:
                if hasattr(self_rtp.pmout, "clear"):
                    self_rtp.pmout.clear()
                else:
                    import io
                    with getattr(self_rtp.pmout, "bufferLock", None) or nullcontext():
                        self_rtp.pmout.buffer = io.BytesIO()
                        self_rtp.pmout.log = {}
        pyVoIP.RTP.RTPClient.clear_tx_buffer = _patched_rtp_clear_tx

        # 2. Patch encode_pcmu to pass through pre-encoded 160-byte G.711 PCMU packets directly
        def _patched_encode_pcmu(self, packet: bytes) -> bytes:
            if not packet or packet == b"\xff" * len(packet) or packet == b"\x80" * len(packet):
                return b"\xff" * 160
            if b"\x80" in packet and packet.endswith(b"\x80"):
                packet = packet.replace(b"\x80", b"\xff")
            if len(packet) < 160:
                return packet + (b"\xff" * (160 - len(packet)))
            return packet[:160]
        pyVoIP.RTP.RTPClient.encode_pcmu = _patched_encode_pcmu
        pyVoIP.RTP.RTPClient.encodePCMU = _patched_encode_pcmu

        # 3. Patch RTP trans thread to use exact 20ms drift-free pacing and send 160-byte PCMU packets
        def _patched_trans(self):
            import time
            import logging
            logger = logging.getLogger(__name__)
            next_time = time.perf_counter()
            frame_duration = 0.020  # 20ms pacing (160 samples @ 8000Hz)
            pkt_count = 0
            
            logger.info(
                f"[RTP_TX] Initialized RTP transmitter | dest={self.outIP}:{self.outPort} | "
                f"src={self.inIP}:{self.inPort} | payload_type=0 (PCMU) | ssrc={self.outSSRC}"
            )
            
            if hasattr(self, "sout") and self.sout:
                try:
                    self.sout.setblocking(True)
                    self.sout.settimeout(0.05)
                except Exception:
                    pass

            while self.NSD:
                payload = self.pmout.read(160)  # Read 160 PCMU bytes (20ms)
                payload = self.encode_packet(payload)  # outputs 160 bytes PCMU
                
                packet = bytearray([0x80])  # V=2, P=0, X=0, CC=0
                packet.append(0)  # PT=0 for PCMU / G.711 u-law
                try:
                    packet.extend(self.outSequence.to_bytes(2, byteorder="big"))
                except OverflowError:
                    self.outSequence = 0
                    packet.extend(self.outSequence.to_bytes(2, byteorder="big"))
                try:
                    packet.extend(self.outTimestamp.to_bytes(4, byteorder="big"))
                except OverflowError:
                    self.outTimestamp = 0
                    packet.extend(self.outTimestamp.to_bytes(4, byteorder="big"))
                    
                packet.extend(self.outSSRC.to_bytes(4, byteorder="big"))
                packet.extend(payload)
                
                try:
                    self.sout.sendto(bytes(packet), (self.outIP, self.outPort))
                    pkt_count += 1
                    if pkt_count % 100 == 1:
                        logger.info(
                            f"[RTP_TX] dest={self.outIP}:{self.outPort} | payload_type=0 | "
                            f"sequence={self.outSequence} | timestamp={self.outTimestamp} | "
                            f"ssrc={self.outSSRC} | payload_bytes={len(payload)}"
                        )
                except (BlockingIOError, OSError) as err:
                    if getattr(err, "winerror", None) == 10035 or getattr(err, "errno", None) in (10035, 11, 35):
                        import select
                        try:
                            _, writable, _ = select.select([], [self.sout], [], 0.005)
                            if writable:
                                self.sout.sendto(bytes(packet), (self.outIP, self.outPort))
                                pkt_count += 1
                        except Exception:
                            pass
                    else:
                        logger.warning(f"[RTP_TX] sendto error: {err}")
                
                self.outSequence = (self.outSequence + 1) & 0xFFFF
                self.outTimestamp = (self.outTimestamp + len(payload)) & 0xFFFFFFFF
                
                next_time += frame_duration
                sleep_time = next_time - time.perf_counter()
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    next_time = time.perf_counter()
        
        pyVoIP.RTP.RTPClient.trans = _patched_trans

        # Monkey-patch pyVoIP to log REGISTER requests
        _orig_register = pyVoIP.SIP.SIPClient.register
        def _patched_register(self):
            import logging
            logging.getLogger(__name__).info(f"\n\n[SIP TRAFFIC OUT] >>> REGISTER \nContact: <sip:{self.username}@{self.myIP}:{self.myPort}>\n")
            return _orig_register(self)
        pyVoIP.SIP.SIPClient.register = _patched_register

        # Monkey-patch pyVoIP to support RFC 2617 qop="auth" Digest Authentication (Asterisk requirement)
        _orig_gen_authorization = pyVoIP.SIP.SIPClient.gen_authorization
        def _patched_gen_authorization(self, request):
            import hashlib
            import random
            
            realm = request.authentication.get("realm", "")
            nonce = request.authentication.get("nonce", "")
            qop = request.authentication.get("qop", None)
            opaque = request.authentication.get("opaque", None)
            
            HA1 = self.username + ":" + realm + ":" + self.password
            HA1 = hashlib.md5(HA1.encode("utf8")).hexdigest()
            
            method = request.headers["CSeq"]["method"]
            uri = "sip:" + self.server + ";transport=UDP"
            HA2 = method + ":" + uri
            HA2 = hashlib.md5(HA2.encode("utf8")).hexdigest()
            
            cnonce = ""
            nc = ""
            qop_val = None
            if qop and "auth" in qop.lower():
                qop_val = "auth"
                nc = "00000001"
                cnonce = ''.join(random.choices("0123456789abcdef", k=16))
                response = (HA1 + ":" + nonce + ":" + nc + ":" + cnonce + ":" + qop_val + ":" + HA2).encode("utf8")
            else:
                response = (HA1 + ":" + nonce + ":" + HA2).encode("utf8")
                
            response_hash = hashlib.md5(response).hexdigest()
            
            # Genius hack to inject missing parameters into pyVoIP's hardcoded string template
            injected = response_hash
            if qop_val:
                injected += f'",qop="{qop_val}",nc="{nc}",cnonce="{cnonce}'
            if opaque:
                injected += f'",opaque="{opaque}'
                
            return injected.encode('utf8')

        pyVoIP.SIP.SIPClient.gen_authorization = _patched_gen_authorization
        pyVoIP.SIP.SIPClient.genAuthorization = _patched_gen_authorization

        _orig_sip_stop = pyVoIP.SIP.SIPClient.stop
        def _patched_sip_stop(self):
            if not getattr(self, "NSD", False):
                return
            self.NSD = False
            if getattr(self, "registerThread", None):
                try:
                    self.registerThread.cancel()
                except Exception:
                    pass
                try:
                    self.deregister()
                except Exception:
                    pass
            try:
                self._close_sockets()
            except Exception:
                pass
        pyVoIP.SIP.SIPClient.stop = _patched_sip_stop
except Exception as patch_err:
    pass

import threading
import os

def _pyvoip_excepthook(args):
    exc_name = getattr(args.exc_type, "__name__", str(args.exc_type))
    if any(ign in exc_name for ign in ("InvalidStateError", "ConnectionResetError", "BrokenPipeError")):
        # Ignore pyVoIP background socket exceptions when the connection is forcibly closed
        return
    if issubclass(args.exc_type, OSError):
        # 10038 = WSAENOTSOCK (socket closed), 10054 = WSAECONNRESET, 10035 = WSAEWOULDBLOCK, 9 = EBADF
        winerr = getattr(args.exc_value, "winerror", None)
        err = getattr(args.exc_value, "errno", None)
        if winerr in (10038, 10054, 10035) or err in (9, 10038, 10054, 10035):
            return
    logger.error(f"Uncaught exception in pyVoIP thread {args.thread.name if args.thread else 'Unknown'}: {args.exc_value}")
    import traceback
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)

threading.excepthook = _pyvoip_excepthook


"""
SIP Registrar — registers the AI Receptionist as a SIP endpoint with FreeSWITCH / Asterisk
and automatically handles inbound calls using the SipCallSession pipeline.

Uses pyVoIP (pure-Python SIP/RTP library) to:
  1. REGISTER with the configured SIP host/username/password
  2. Auto-answer inbound INVITE (calls)
  3. Bridge bidirectional RTP audio to the STT→LLM→TTS pipeline
  4. Send SIP BYE on call end

The registrar runs in a background thread and provides status to the API layer.
"""

import asyncio
import logging
import threading
import time
import os
import socket
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable

from src.services.sip_call_handler import SipCallSession, terminate_call

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Multi-Tenant Registrar State (Isolated by Organization ID)
# ─────────────────────────────────────────────────────────────
_registrars: Dict[int, "SipRegistrar"] = {}
_registrar_lock = threading.RLock()


def get_registrar(organization_id: Optional[int] = None) -> Optional["SipRegistrar"]:
    """
    Return the active registrar for the given organization_id.
    If organization_id is None, returns the primary/first active registrar
    for backward compatibility with single-tenant callers.
    """
    with _registrar_lock:
        if organization_id is not None:
            return _registrars.get(int(organization_id))
        if 1 in _registrars:
            return _registrars[1]
        if _registrars:
            return next(iter(_registrars.values()))
        return None


def get_all_registrars() -> Dict[int, "SipRegistrar"]:
    """Return a snapshot of all currently active tenant registrars."""
    with _registrar_lock:
        return dict(_registrars)


def set_registrar(r: Optional["SipRegistrar"], organization_id: Optional[int] = None):
    """Register or remove a tenant registrar instance."""
    with _registrar_lock:
        org_id = int(organization_id or getattr(r, "organization_id", 1) or 1)
        if r is None:
            _registrars.pop(org_id, None)
        else:
            _registrars[org_id] = r


# ─────────────────────────────────────────────────────────────
# SIP Registrar (Tenant-Aware UA Instance)
# ─────────────────────────────────────────────────────────────
class SipRegistrar:
    """
    Manages the SIP UA lifecycle for the AI Receptionist.
    Compatible with FreeSWITCH, Asterisk, and external cloud SIP trunks.
    Isolated per tenant organization.
    """
    _known_trunk_dids: set = set()

    def __init__(
        self,
        sip_host: str,
        sip_username: str,
        sip_password: str,
        sip_port: int = 5060,
        did_number: Optional[str] = None,
        organization_id: int = 1,
    ):
        self.sip_host = sip_host.strip()
        self.sip_username = sip_username.strip()
        self.sip_password = sip_password.strip()
        self.sip_port = int(sip_port or 5060)
        self.did_number = did_number.strip() if did_number else None
        self.organization_id = int(organization_id or 1)
        # Allocate dedicated base port per tenant to avoid binding race
        self.local_bind_port = 5080 + (2 * ((self.organization_id - 1) % 50))

        self.status = "unregistered"           # unregistered | registering | registered | error
        self.detail = "Not yet registered"
        self.registered_at: Optional[datetime] = None
        self.last_attempt_at: Optional[datetime] = None
        self.error_message: Optional[str] = None
        self._stop_event = threading.Event()
        self._phone: Optional[Any] = None      # pyVoIP VoIPPhone instance
        self._thread: Optional[threading.Thread] = None

    # ──────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """Check whether the registrar background thread is active."""
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    def matches_config(self, host: str, username: str, port: int = 5060, did: Optional[str] = None) -> bool:
        """Check whether this running registrar matches the target configuration."""
        return (
            self.sip_host == (host or "").strip()
            and self.sip_username == (username or "").strip()
            and self.sip_port == int(port or 5060)
            and (self.did_number or "") == (did or "").strip()
        )

    def is_registered(self) -> bool:
        """Strict check confirming whether the SIP UA is confirmed registered by provider response."""
        if self.status != "registered":
            return False
        if self._phone:
            st = getattr(self._phone.get_status(), "name", "")
            return st == "REGISTERED"
        return False

    def _get_local_ip(self) -> str:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "0.0.0.0"

    def start(self):
        """Start the SIP registrar in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.info(f"[SIP REG] Org #{self.organization_id} registrar already running, skipping start.")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"sip-reg-org-{self.organization_id}"
        )
        self._thread.start()
        logger.info(f"[SIP REG] Org #{self.organization_id} registrar thread started for {self.sip_username}@{self.sip_host}:{self.sip_port}")

    def stop(self):
        """Gracefully stop the registrar and tear down the SIP phone."""
        self._stop_event.set()
        self._teardown_phone()
        # Brief wait to allow daemon threads to observe stop_event and exit
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info(f"[SIP REG] Org #{self.organization_id} registrar stopped.")

    def get_status(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "organization_id": self.organization_id,
            "sip_host": self.sip_host,
            "sip_username": self.sip_username,
            "sip_port": self.sip_port,
            "did_number": self.did_number,
            "registered_at": self.registered_at.isoformat() if self.registered_at else None,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "error_message": self.error_message,
        }

    def wait_for_status(self, target_statuses=("registered", "error"), timeout: float = 3.0) -> str:
        """Wait until registrar status transitions to one of target_statuses or times out."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.status in target_statuses:
                return self.status
            time.sleep(0.05)
        return self.status

    def make_outbound_call(self, to_number: str, call_sid: Optional[str] = None, db_call_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Originate an outbound SIP call using pyVoIP.
        After the remote party answers, automatically sets up the full
        AI audio bridge (STT → LLM → TTS → RTP) in a background thread.
        """
        to_number = to_number.strip()

        # Do NOT attempt any pyVoIP operation after server shutdown
        if self._stop_event.is_set():
            logger.info(f"[SIP REG] Ignoring outbound call to {to_number} — registrar is stopped.")
            return {"success": False, "method": "none", "message": "Registrar stopped."}

        logger.info(f"[SIP REG] Originating outbound SIP call to {to_number} via {self.sip_host}:{self.sip_port} (sid={call_sid})")

        # 1. Try direct pyVoIP call if phone is initialized
        if self._phone and hasattr(self._phone, "call"):
            try:
                call = self._phone.call(to_number)
                call_id = call_sid or getattr(call, "call_id", getattr(call, "callID", f"out_{to_number}"))
                logger.info(f"[SIP REG] pyVoIP outbound INVITE sent to {to_number} (callID: {call_id})")

                # Launch a watcher thread that waits for the remote party to answer,
                # then sets up the full AI audio bridge.
                threading.Thread(
                    target=self._on_outbound_call_answered,
                    args=(call, to_number),
                    kwargs={"call_sid": call_sid, "db_call_id": db_call_id},
                    daemon=True,
                    name=f"outbound-watcher-{call_id}",
                ).start()

                return {
                    "success": True,
                    "method": "pyvoip",
                    "call_id": str(call_id),
                    "message": f"Outbound SIP call placed to {to_number} via pyVoIP. Waiting for answer..."
                }
            except Exception as pyvoip_err:
                logger.warning(f"[SIP REG] pyVoIP outbound call error: {pyvoip_err}")

        # If phone is not alive, attempt re-registration — but only if registrar is still running
        if not self._is_phone_alive():
            if self._stop_event.is_set():
                logger.info("[SIP REG] Registrar stopped; skipping re-registration.")
                return {"success": False, "method": "none", "message": "Registrar stopped."}
            logger.info("[SIP REG] SIP phone instance inactive/closed socket. Attempting re-registration...")
            try:
                self._register()
            except Exception as reg_err:
                logger.warning(f"[SIP REG] Re-registration attempt failed: {reg_err}")

        if self._phone and hasattr(self._phone, "call"):
            try:
                call = self._phone.call(to_number)
                call_id = call_sid or getattr(call, "call_id", getattr(call, "callID", f"out_{to_number}"))
                logger.info(f"[SIP REG] pyVoIP outbound INVITE sent after re-reg to {to_number} (callID: {call_id})")

                threading.Thread(
                    target=self._on_outbound_call_answered,
                    args=(call, to_number),
                    kwargs={"call_sid": call_sid, "db_call_id": db_call_id},
                    daemon=True,
                    name=f"outbound-watcher-{call_id}",
                ).start()

                return {
                    "success": True,
                    "method": "pyvoip",
                    "call_id": str(call_id),
                    "message": f"Outbound SIP call placed to {to_number} via pyVoIP. Waiting for answer..."
                }
            except Exception as pyvoip_err:
                logger.warning(f"[SIP REG] pyVoIP second outbound attempt error: {pyvoip_err}")

        # 2. Try Asterisk PJSIP CLI origination (if Asterisk / WSL is running)
        try:
            import subprocess
            cmd = f'asterisk -rx "channel originate PJSIP/{to_number}@{self.sip_host} extension 100@from-trunk"'
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                logger.info(f"[SIP REG] Asterisk CLI originate executed for {to_number}")
                return {
                    "success": True,
                    "method": "asterisk_pjsip",
                    "message": f"Outbound call initiated via Asterisk PJSIP to {to_number}."
                }
        except Exception as ast_err:
            logger.debug(f"[SIP REG] Asterisk CLI origination note: {ast_err}")

        # 3. Fallback: Active AI Call Session Bridge & Socket broadcast
        logger.info(f"[SIP REG] Initializing AI Call Session fallback for outbound call to {to_number}")
        try:
            from src.services.sip_call_handler import SipCallSession, register_call
            import uuid
            call_sid = f"out_sip_{uuid.uuid4().hex[:10]}"
            session = SipCallSession(
                caller_phone=to_number,
                caller_name=f"Outbound → {to_number}",
                organization_id=1,
            )
            session.call_sid = call_sid
            session.session_id = f"sip_session_{call_sid}"
            register_call(session)
            
            # Start greeting asynchronously in background
            def _start_ai_bg():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(session._send_opening_greeting())
                loop.close()

            threading.Thread(target=_start_ai_bg, daemon=True, name=f"ai-outbound-{call_sid}").start()

            return {
                "success": True,
                "method": "ai_session_bridge",
                "call_id": call_sid,
                "message": f"Outbound AI call session active for {to_number}."
            }
        except Exception as bridge_err:
            logger.error(f"[SIP REG] Outbound AI session creation error: {bridge_err}")

        return {
            "success": False,
            "method": "none",
            "message": f"Could not initiate outbound call to {to_number}."
        }


    def _mark_outbound_call_failed(self, call_sid: Optional[str] = None, db_call_id: Optional[int] = None, reason: str = "FAILED"):
        """Cleanly mark un-answered or rejected outbound calls as failed in DB."""
        try:
            from src.config.database import SessionLocal
            from src.models.call_log import CallLog, CallType
            db = SessionLocal()
            try:
                call = None
                if db_call_id:
                    call = db.query(CallLog).filter(CallLog.id == db_call_id).first()
                if not call and call_sid:
                    call = db.query(CallLog).filter(CallLog.call_sid == call_sid).first()
                if call and call.status in ("in-progress", "initiated"):
                    call.status = "failed"
                    call.duration = "00:00"
                    call.call_type = CallType.OUTBOUND
                    call.recording_status = "NOT_RECORDED"
                    call.recording_error = f"Outbound call ended before answer: {reason}"
                    call.receptionist_state = f"SIP OUTBOUND - {reason}"
                    db.commit()
                    logger.info(f"[SIP OUTBOUND] Cleanly marked call #{call.id} ({call.call_sid}) as {call.status} ({reason})")
            finally:
                db.close()
        except Exception as exc:
            logger.warning(f"[SIP OUTBOUND] Failed to mark call failed: {exc}")

    def _on_outbound_call_answered(self, call, to_number: str, timeout: int = 60, call_sid: Optional[str] = None, db_call_id: Optional[int] = None):
        """
        Wait for the outbound call to be answered, then set up the AI audio bridge.
        Runs in a background thread.
        """
        from pyVoIP.VoIP import CallState
        logger.info(f"[SIP OUTBOUND] Waiting for {to_number} to answer (timeout={timeout}s, sid={call_sid})...")

        deadline = time.time() + timeout
        while time.time() < deadline:
            state = getattr(call, "state", None)
            if state == CallState.ANSWERED:
                logger.info(f"[SIP OUTBOUND] {to_number} ANSWERED — starting AI audio bridge.")
                self._run_outbound_audio_bridge(call, to_number, call_sid=call_sid, db_call_id=db_call_id)
                return
            elif state == CallState.ENDED:
                logger.warning(f"[SIP OUTBOUND] Call to {to_number} ended before being answered "
                               f"(SIP 404/480/503 or remote hangup).")
                self._mark_outbound_call_failed(call_sid=call_sid, db_call_id=db_call_id, reason="NO_ANSWER_OR_BUSY")
                return
            time.sleep(0.5)

        logger.warning(f"[SIP OUTBOUND] Call to {to_number} timed out waiting for answer.")
        self._mark_outbound_call_failed(call_sid=call_sid, db_call_id=db_call_id, reason="TIMEOUT_NO_ANSWER")
        try:
            call.hangup()
        except Exception:
            pass

    def _run_outbound_audio_bridge(self, call, to_number: str, call_sid: Optional[str] = None, db_call_id: Optional[int] = None):
        """
        Create a SipCallSession and run the full audio bridge for an outbound call.
        Mirrors _on_inbound_call but for calls we placed.
        """
        try:
            from src.services.sip_call_handler import SipCallSession
            from src.models.call_log import CallType
            from pyVoIP.VoIP import CallState

            def audio_send(pcm_bytes: bytes):
                """Send 8-bit unsigned 8000Hz PCM audio back via pyVoIP RTP."""
                try:
                    if call.state == CallState.ANSWERED:
                        if hasattr(call, "write_audio"):
                            call.write_audio(pcm_bytes)
                        elif hasattr(call, "writeAudio"):
                            call.writeAudio(pcm_bytes)
                except Exception as exc:
                    logger.warning(f"[SIP OUTBOUND RTP OUT] Send error: {exc}")

            def audio_clear():
                """Flush pyVoIP transmit buffer for instant barge-in."""
                try:
                    if hasattr(call, "RTPClients") and call.RTPClients:
                        for client in call.RTPClients:
                            if hasattr(client, "pmout") and client.pmout:
                                try:
                                    with client.pmout.bufferLock:
                                        import io
                                        client.pmout.buffer = io.BytesIO()
                                        client.pmout.log = {}
                                        client.pmout.offset = getattr(client, "outOffset", 0)
                                except Exception:
                                    pass
                    if hasattr(call, "rtp") and call.rtp:
                        if hasattr(call.rtp, "clear"):
                            call.rtp.clear()
                        elif hasattr(call.rtp, "clear_tx_buffer"):
                            call.rtp.clear_tx_buffer()
                except Exception as exc:
                    logger.warning(f"[SIP OUTBOUND BARGE-IN CLEAR] Error: {exc}")

            resolved_org_id = getattr(self, "organization_id", None)
            resolved_agent_id = None
            resolved_script_id = None
            resolved_knowledge_id = None
            resolved_context_len = 0
            resolved_source_count = 0

            try:
                from src.config.database import SessionLocal
                from src.models.sip_config import SipConfig
                from src.models.knowledge_doc import KnowledgeDocument
                from src.services.rag_service import rag_service

                db = SessionLocal()
                try:
                    sip_cfg = db.query(SipConfig).filter(
                        (SipConfig.sip_host == self.sip_host) |
                        (SipConfig.sip_username == self.sip_username)
                    ).first()
                    if sip_cfg and sip_cfg.organization_id:
                        resolved_org_id = sip_cfg.organization_id

                    # Resolve Active Script Document (Outbound)
                    script_doc = db.query(KnowledgeDocument).filter(
                        KnowledgeDocument.ai_usage == "Active",
                        (KnowledgeDocument.document_type.in_(["OUTBOUND_SCRIPT", "SCRIPT"])) |
                        (KnowledgeDocument.title.ilike("%outbound%")) |
                        (KnowledgeDocument.scope == "outbound")
                    ).filter(
                        KnowledgeDocument.organization_id == resolved_org_id
                    ).order_by(KnowledgeDocument.updated_at.desc()).first()
                    if script_doc:
                        resolved_script_id = str(script_doc.id)

                    # Resolve Active Knowledge / RAG Document (Outbound)
                    active_doc_id = rag_service._resolve_active_document_id(organization_id=resolved_org_id, scope="outbound")
                    if active_doc_id:
                        resolved_knowledge_id = str(active_doc_id)
                finally:
                    db.close()
            except Exception as e:
                logger.warning(f"[OUTBOUND RESOLVE ERROR] {e}")

            session = SipCallSession(
                caller_phone=to_number,
                caller_name=f"Outbound → {to_number}",
                call_type=CallType.OUTBOUND,
                call_sid=call_sid,
                organization_id=resolved_org_id,
                audio_send_callback=audio_send,
                audio_clear_callback=audio_clear,
                agent_id=resolved_agent_id,
                active_script_id=resolved_script_id,
                active_knowledge_id=resolved_knowledge_id,
            )
            if db_call_id:
                session.db_call_id = db_call_id
            session.call_type = CallType.OUTBOUND
            session.pyvoip_call = call

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def run_session():
                logger.info(f"[SIP OUTBOUND SESSION] Session {session.call_sid} starting.")
                rtp_task = asyncio.create_task(self._rtp_receive_loop(call, session))
                ai_task = asyncio.create_task(session.process_audio_loop())
                await session.start()

                from pyVoIP.VoIP import CallState
                while not session.ended and getattr(call, "state", None) == CallState.ANSWERED:
                    await asyncio.sleep(0.2)

                logger.info(f"[SIP OUTBOUND SESSION] Session {session.call_sid} loop ended.")
                from src.services.sip_call_handler import terminate_call
                await terminate_call(session.call_sid, reason=getattr(session, "end_reason", "REMOTE_HANGUP"))

                for task in (rtp_task, ai_task):
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

            loop.run_until_complete(run_session())
            loop.close()

        except Exception as exc:
            logger.error(f"[SIP OUTBOUND] Audio bridge error for {to_number}: {exc}")
            try:
                call.hangup()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # Background registration loop
    # ──────────────────────────────────────────────────────────

    def _run_loop(self):
        """Runs in background thread: register, monitor, auto-reconnect."""
        retry_delay = 15  # seconds between retries
        while not self._stop_event.is_set():
            self.last_attempt_at = datetime.now(timezone.utc)
            try:
                self._register()
                # Grace period — let pyVoIP fully stabilize before monitoring
                self._stop_event.wait(timeout=10)
                # Poll keep-alive every 30s (not 5s) to reduce re-registration churn
                while not self._stop_event.is_set():
                    self._stop_event.wait(timeout=30)
                    if not self._stop_event.is_set() and not self._is_phone_alive():
                        logger.warning("[SIP REG] Phone became unresponsive, re-registering...")
                        self.status = "unregistered"
                        self.detail = "Lost registration, attempting re-register..."
                        break
            except Exception as exc:
                self.status = "error"
                self.error_message = str(exc)
                self.detail = f"Registration failed: {exc}"
                logger.error(f"[SIP REG] Registration error: {exc}")
                self._teardown_phone()

            if not self._stop_event.is_set():
                logger.info(f"[SIP REG] Retrying in {retry_delay}s...")
                self._stop_event.wait(timeout=retry_delay)

    def _get_public_ip(self) -> str:
        """Fetch the public IP of this machine for NAT traversal with non-blocking cache."""
        cached = getattr(self, "_cached_public_ip", None)
        if cached:
            return cached
        import urllib.request
        try:
            req = urllib.request.Request("https://api.ipify.org", headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=1.0) as response:
                ip = response.read().decode('utf-8').strip()
                if ip:
                    self._cached_public_ip = ip
                    return ip
        except Exception:
            pass
        return "210.18.176.97"

    def _patch_pyvoip_for_nat(self, stun_server_uri: str = None):
        """
        Monkey-patch pyVoIP's start methods so it binds to 0.0.0.0 internally,
        but allows us to pass a public IP as `myIP` which pyVoIP will use in
        SIP Contact headers and SDP bodies. If a STUN server is provided,
        it accurately discovers the mapped external IP and Port for the SIP socket.
        """
        import pyVoIP.SIP as sip
        import pyVoIP.RTP as rtp
        import socket
        import struct
        import os
        from threading import Timer

        if getattr(sip.SIPClient, "_nat_patched", False):
            return

        def get_stun_mapped_address(sock, stun_host, stun_port):
            import os
            import struct
            import socket
            try:
                transaction_id = os.urandom(12)
                req = struct.pack('!HH', 0x0001, 0) + struct.pack('!I', 0x2112A442) + transaction_id
                sock.sendto(req, (socket.gethostbyname(stun_host), stun_port))
                sock.settimeout(2.0)
                data, _ = sock.recvfrom(1024)
                if len(data) >= 20:
                    msg_type, length, cookie = struct.unpack('!HHI', data[:8])
                    if msg_type == 0x0101:
                        offset = 20
                        while offset < 20 + length:
                            attr_type, attr_length = struct.unpack('!HH', data[offset:offset+4])
                            if attr_type == 0x0020: # XOR-MAPPED-ADDRESS
                                _, port, ip = struct.unpack('!xBH4s', data[offset+4:offset+12])
                                port = port ^ 0x2112
                                ip = socket.inet_ntoa(struct.pack('!I', struct.unpack('!I', ip)[0] ^ cookie))
                                return ip, port
                            elif attr_type == 0x0001: # MAPPED-ADDRESS
                                _, port, ip = struct.unpack('!xBH4s', data[offset+4:offset+12])
                                return socket.inet_ntoa(ip), port
                            offset += 4 + attr_length
            except Exception as e:
                logger.warning(f"[SIP STUN] STUN request failed: {e}")
            finally:
                sock.settimeout(None) # Reset to blocking
            return None, None

        def monkey_sip_start(self):
            import socket
            if self.NSD:
                raise RuntimeError("Attempted to start already started SIPClient")
            self.NSD = True
            self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except Exception:
                pass
            self.s.bind(('0.0.0.0', self.myPort))
            self.out = self.s
            
            # STUN integration for exact NAT mapping (only if explicitly enabled, else let Asterisk rewrite_contact handle NAT)
            if stun_server_uri and stun_server_uri.startswith("stun:") and os.environ.get("SIP_USE_STUN", "").lower() in ("true", "1"):
                try:
                    parts = stun_server_uri.replace("stun:", "").split(":")
                    stun_host = parts[0]
                    stun_port = int(parts[1]) if len(parts) > 1 else 3478
                    logger.info(f"[SIP STUN] Discovering external mapping via {stun_host}:{stun_port}...")
                    ext_ip, ext_port = get_stun_mapped_address(self.s, stun_host, stun_port)
                    if ext_ip and ext_port:
                        logger.info(f"[SIP STUN] Discovered mapped address: {ext_ip}:{ext_port}")
                        self.myIP = ext_ip
                        self.myPort = ext_port
                except Exception as e:
                    logger.error(f"[SIP STUN] Failed to resolve STUN: {e}")

            # Set registration expiry to 180s to maintain clean registration without flooding
            self.default_expires = 180
            self.register()
            
            t = Timer(1, self.recv_loop)
            t.name = "SIP Receive"
            t.start()
            
            # SIP UDP Keep-Alive (RFC 5626) to prevent NAT hole from closing.
            # Sends a blank UDP payload to the SIP server every 15 seconds.
            def keepalive_loop():
                import time
                while self.NSD:
                    try:
                        self.s.sendto(b'\r\n\r\n', (self.server, self.port))
                    except Exception:
                        pass
                    time.sleep(15)
                    
            from threading import Thread
            k = Thread(target=keepalive_loop, daemon=True, name="SIP KeepAlive")
            k.start()
        
        sip.SIPClient.start = monkey_sip_start
        sip.SIPClient._nat_patched = True

        # Guard against pyVoIP UnboundLocalError on socket close / BlockingIOError
        orig_recv = sip.SIPClient.recv
        def safe_recv(self_sip):
            raw = None
            try:
                raw = self_sip.s.recv(8192)
                if not raw or raw in (b"\x00\x00\x00\x00", b"\r\n\r\n", b"\r\n"):
                    return
                raw_str = raw.decode("utf-8", errors="ignore")

                # 1. Immediately handle Asterisk qualify OPTIONS pings
                if raw_str.strip().startswith("OPTIONS"):
                    try:
                        import re
                        cid_m = re.search(r"Call-ID:\s*(.*?)\r?\n", raw_str, re.I)
                        cseq_m = re.search(r"CSeq:\s*(.*?)\r?\n", raw_str, re.I)
                        cid = cid_m.group(1).strip() if cid_m else "<unknown>"
                        cseq = cseq_m.group(1).strip() if cseq_m else "<unknown>"
                        try:
                            msg = sip.SIPMessage(raw)
                            ok_resp = self_sip.gen_ok(msg)
                        except Exception:
                            via_m = re.search(r"Via:\s*(.*?)\r?\n", raw_str, re.I)
                            from_m = re.search(r"From:\s*(.*?)\r?\n", raw_str, re.I)
                            to_m = re.search(r"To:\s*(.*?)\r?\n", raw_str, re.I)
                            import uuid
                            to_str = to_m.group(1).strip() if to_m else ""
                            if ";tag=" not in to_str.lower():
                                to_str += f";tag={uuid.uuid4().hex[:8]}"
                            via_str = via_m.group(1).strip() if via_m else ""
                            if ";rport=" not in via_str and ";rport" in via_str:
                                via_str = via_str.replace(";rport", f";rport={self_sip.port};received={self_sip.server}")
                            elif ";rport=" not in via_str:
                                via_str += f";rport={self_sip.port};received={self_sip.server}"
                            ok_resp = (
                                f"SIP/2.0 200 OK\r\n"
                                f"Via: {via_str}\r\n"
                                f"From: {from_m.group(1).strip() if from_m else ''}\r\n"
                                f"To: {to_str}\r\n"
                                f"Call-ID: {cid}\r\n"
                                f"CSeq: {cseq}\r\n"
                                f"Contact: <sip:{self_sip.username}@{self_sip.myIP}:{self_sip.myPort}>\r\n"
                                f"Allow: INVITE, ACK, CANCEL, OPTIONS, BYE\r\n"
                                f"User-Agent: pyVoIP\r\n"
                                f"Content-Length: 0\r\n\r\n"
                            )
                        self_sip.s.sendto(ok_resp.encode("utf8"), (self_sip.server, self_sip.port))
                        logger.info(f"[SIP OPTIONS] Replied 200 OK to Asterisk qualify ping (Call-ID: {cid}, CSeq: {cseq})")
                    except Exception as opt_err:
                        logger.error(f"[SIP OPTIONS] Failed to answer OPTIONS ping: {opt_err}")
                    return

                # 2. Log incoming INVITE from Asterisk
                if raw_str.strip().startswith("INVITE"):
                    import re
                    cid_m = re.search(r"Call-ID:\s*(.*?)\r?\n", raw_str, re.I)
                    from_m = re.search(r"From:\s*(.*?)\r?\n", raw_str, re.I)
                    to_m = re.search(r"To:\s*(.*?)\r?\n", raw_str, re.I)
                    cid = cid_m.group(1).strip() if cid_m else "<unknown>"
                    frm = from_m.group(1).strip() if from_m else "<unknown>"
                    to = to_m.group(1).strip() if to_m else "<unknown>"
                    logger.info(f"[INBOUND INVITE RECEIVED] Call-ID={cid} From={frm} To={to}")

                # 3. Log incoming in-dialog responses and signals
                first_line = raw_str.strip().split("\n")[0].strip()
                if any(k in first_line for k in ("200 OK", "ACK", "BYE", "180 Ringing", "183 Session")):
                    import re
                    cid_m = re.search(r"Call-ID:\s*(.*?)\r?\n", raw_str, re.I)
                    cseq_m = re.search(r"CSeq:\s*(.*?)\r?\n", raw_str, re.I)
                    cid = cid_m.group(1).strip() if cid_m else "<unknown>"
                    cseq = cseq_m.group(1).strip() if cseq_m else "<unknown>"
                    logger.info(f"[SIP SIGNAL IN] {first_line} | Call-ID={cid} | CSeq={cseq}")

                try:
                    message = sip.SIPMessage(raw)
                    sip.debug(message.summary())
                    self_sip.parse_message(message)
                except Exception as ex:
                    sip.debug(f"Error on header parsing: {ex}")
            except sip.SIPParseError as e:
                if "SIP Version" in str(e):
                    request = self_sip.gen_sip_version_not_supported(message)
                    self_sip.out.sendto(
                        request.encode("utf8"), (self_sip.server, self_sip.port)
                    )
                else:
                    sip.debug(f"SIPParseError in SIP.recv: {type(e)}, {e}")
            except BlockingIOError:
                raise
            except OSError:
                # Catch WinError 10038 / 10054 / WSAENOTSOCK when shutting down
                return
            except Exception as e:
                if not getattr(self_sip, "NSD", False):
                    return
                raw_str = str(raw, 'utf8', errors='replace') if raw is not None else "<no data>"
                sip.debug(f"SIP.recv error: {type(e)}, {e}\n\n{raw_str}")
                if getattr(pyVoIP, "DEBUG", False):
                    raise
        sip.SIPClient.recv = safe_recv

        def monkey_rtp_start(self):
            self.sin = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.sin.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except Exception:
                pass
            self.sin.bind(('0.0.0.0', self.inPort))
            self.sin.setblocking(False)
            
            # CRITICAL FOR NAT: Use the exact same socket for sending and receiving.
            # This enables Symmetric RTP (the router maps the outbound traffic's source port 
            # to our bound inPort, so inbound audio from the trunk can reach us).
            self.sout = self.sin

            r = Timer(0, self.recv)
            r.name = "RTP Receiver"
            r.start()
            t = Timer(0, self.trans)
            t.name = "RTP Transmitter"
            t.start()
            
        rtp.RTPClient.start = monkey_rtp_start

    def _register(self):
        """Perform SIP REGISTER and set up call handler."""
        if self._stop_event.is_set():
            logger.info("[SIP REG] _register() called after stop — skipping.")
            return
        self._teardown_phone()  # Clean up any lingering socket handles before binding
        self.status = "registering"
        self.detail = f"Registering with {self.sip_username}@{self.sip_host}:{self.sip_port}..."
        logger.info(f"[SIP REG] {self.detail}")

        try:
            from pyVoIP.VoIP import VoIPPhone, CallState, InvalidStateError

            # Parse STUN Server from settings
            stun_server = os.environ.get("STUN_SERVER", "")

            # Patch pyVoIP to bind to 0.0.0.0 while using public IP/STUN in headers
            self._patch_pyvoip_for_nat(stun_server_uri=stun_server)
            
            public_ip = getattr(self, "_cached_public_ip", None)
            if not public_ip:
                public_ip = self._get_public_ip()
                self._cached_public_ip = public_ip
            
            local_ip = self._get_local_ip()
            chosen_ip = public_ip if (public_ip and public_ip != "0.0.0.0") else local_ip
            logger.info(f"[SIP REG] Discovered public IP: {public_ip}, using {chosen_ip} in SIP headers.")
            self.myIP = chosen_ip

            # Derive port candidates per organization to ensure isolation without port collision
            base_port = 5080 + (2 * ((self.organization_id - 1) % 50))
            candidate_ports = [base_port + (offset * 100) for offset in range(5)]
            phone_started = False
            for local_bind_port in candidate_ports:
                try:
                    self.local_bind_port = local_bind_port
                    phone = VoIPPhone(
                        server=self.sip_host,
                        port=self.sip_port,
                        username=self.sip_username,
                        password=self.sip_password,
                        myIP=self.myIP,
                        sipPort=self.local_bind_port,
                        callCallback=self._on_inbound_call,
                    )
                    phone.start()
                    # Registration expiry set to 180 seconds (3 minutes) for healthy keep-alive without flooding
                    phone.sip.default_expires = 180

                    self._phone = phone
                    phone_started = True
                    break
                except Exception as bind_err:
                    logger.warning(f"[SIP REG] Org {self.organization_id} bind port {local_bind_port} note: {bind_err}")
                    continue

            if phone_started:
                deadline = time.time() + 6.0
                while time.time() < deadline:
                    if self._is_phone_alive():
                        st = getattr(self._phone.get_status(), "name", "")
                        if st in ("REGISTERED", "FAILED", "UNREGISTERED"):
                            break
                    time.sleep(0.05)

            is_alive = self._is_phone_alive()
            st_name = getattr(self._phone.get_status(), "name", "") if (phone_started and is_alive) else ""

            if phone_started and is_alive and st_name == "REGISTERED":
                self.status = "registered"
                self.registered_at = datetime.now(timezone.utc)
                self.detail = f"Registered successfully with {self.sip_host}:{self.sip_port}"
                self.error_message = None
                self._persist_status("registered", self.detail)
                logger.info(f"[SIP REG] *** REGISTERED (Org {self.organization_id}) *** {self.sip_username}@{self.sip_host}")
            elif not phone_started:
                self.status = "error"
                self.detail = f"Failed to bind local SIP socket for organization {self.organization_id} on ports {candidate_ports}"
                self.error_message = "Port binding failure"
                self._persist_status("error", self.detail)
                logger.error(f"[SIP REG] Port binding failed for org {self.organization_id}")
            elif st_name in ("FAILED", "UNREGISTERED") or not is_alive:
                self.status = "error"
                self.detail = f"SIP registration rejected by {self.sip_host}:{self.sip_port} (Status: {st_name or 'DISCONNECTED'}). Check credentials or server IP."
                self.error_message = f"Registration failed with status {st_name or 'DISCONNECTED'}"
                self._persist_status("error", self.detail)
                logger.warning(f"[SIP REG] Registration failed for org {self.organization_id}: {self.detail}")
                self._teardown_phone()
            else:
                self.status = "error"
                self.detail = f"SIP registration timed out connecting to {self.sip_host}:{self.sip_port}"
                self.error_message = "Registration timed out"
                self._persist_status("error", self.detail)
                logger.warning(f"[SIP REG] Registration timed out for org {self.organization_id}: {self.detail}")
                self._teardown_phone()

        except ImportError:
            # pyVoIP not installed — fallback to webhook-only mode
            self.status = "error"
            self.detail = "pyVoIP not installed — running in webhook-only mode."
            self.error_message = "Install pyVoIP: pip install pyVoIP"
            self._persist_status("error", self.detail)
            logger.error("[SIP REG] pyVoIP import failed. Install with: pip install pyVoIP")
            raise

    def _on_inbound_call(self, call):
        """Called by pyVoIP when an inbound SIP INVITE arrives."""
        from pyVoIP.VoIP import CallState

        try:
            req_headers = getattr(call, "request", None) and getattr(call.request, "headers", {}) or {}
            raw_bytes = getattr(call, "request", None) and getattr(call.request, "raw", b"") or b""
            raw_sip = raw_bytes.decode("utf-8", errors="ignore") if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes or "")
            
            call_id = req_headers.get("Call-ID", "Unknown")
            if isinstance(call_id, dict):
                call_id = call_id.get("raw", str(call_id))
            
            logger.info(f"[SIP INBOUND HEADERS] req_headers={req_headers} raw_len={len(raw_sip)}")
            
            # Dynamic Inbound Context Resolution
            resolved_org_id = self.organization_id or 1
            resolved_agent_id = None
            resolved_script_id = None
            resolved_knowledge_id = None
            resolved_context_len = 0
            resolved_source_count = 0

            try:
                from src.config.database import SessionLocal
                from src.models.sip_config import SipConfig
                from src.models.knowledge_doc import KnowledgeDocument
                from src.services.rag_service import rag_service

                db = SessionLocal()
                try:
                    sip_cfg = db.query(SipConfig).filter(SipConfig.organization_id == resolved_org_id).first()
                    if not sip_cfg:
                        sip_cfg = db.query(SipConfig).filter(
                            (SipConfig.sip_host == self.sip_host) |
                            (SipConfig.sip_username == self.sip_username)
                        ).first()
                        if sip_cfg and sip_cfg.organization_id:
                            resolved_org_id = sip_cfg.organization_id

                    # Resolve Active Script Document (Inbound only - strictly exclude outbound scripts)
                    script_doc = db.query(KnowledgeDocument).filter(
                        KnowledgeDocument.document_type.in_(["INBOUND_SCRIPT", "SCRIPT"]),
                        KnowledgeDocument.ai_usage == "Active",
                        KnowledgeDocument.document_type.notin_(["OUTBOUND_SCRIPT", "CAMPAIGN_SCRIPT"]),
                        ~KnowledgeDocument.title.ilike("%outbound%"),
                        KnowledgeDocument.scope != "outbound",
                    ).filter(
                        KnowledgeDocument.organization_id == resolved_org_id
                    ).order_by(KnowledgeDocument.updated_at.desc()).first()
                    if script_doc:
                        resolved_script_id = str(script_doc.id)

                    # Resolve Active Knowledge / RAG Document (Inbound only)
                    active_doc_id = rag_service._resolve_active_document_id(organization_id=resolved_org_id, scope="inbound")
                    if active_doc_id:
                        resolved_knowledge_id = str(active_doc_id)
                        master_ctx = rag_service.get_organization_master_context(resolved_org_id, document_id=active_doc_id)
                        resolved_context_len = len(master_ctx)
                        resolved_source_count = 1
                finally:
                    db.close()
            except Exception as e:
                logger.warning(f"[INBOUND RESOLVE ERROR] {e}")

            # Extract raw caller phone & name from SIP INVITE headers (strictly avoiding callee DID)
            caller_phone, raw_name = self._extract_customer_phone_and_name(
                req_headers,
                trunk_username=self.sip_username,
                did_number=self.did_number,
                raw_sip=raw_sip
            )

            # Dynamically resolve matching Customer / Contact record from database
            customer_info = self._resolve_customer_identity(
                phone=caller_phone,
                organization_id=resolved_org_id,
                initial_name=raw_name
            )
            resolved_phone = customer_info["phone"]
            resolved_name  = customer_info["name"]

            logger.info(f"[INBOUND CALLBACK] Call received: Call-ID={call_id} -> Caller Phone={resolved_phone} Name={resolved_name} (Known: {customer_info['is_known']})")

            # Persist caller debug log for verification
            try:
                with open("inbound_debug.log", "a", encoding="utf-8") as _df:
                    _df.write(f"\n[{datetime.now()}] Call-ID={call_id}\n")
                    _df.write(f"  REQ_HEADERS: {req_headers}\n")
                    _df.write(f"  RAW_SIP:\n{raw_sip}\n")
                    _df.write(f"  EXTRACTED: phone='{resolved_phone}' name='{resolved_name}'\n")
            except Exception:
                pass

            call.answer()
            logger.info(f"[INBOUND ANSWERED] Call answered: Call-ID={call_id} state={call.state}")

            logger.info(
                f"\n[CALL_CONTEXT]"
                f"\n  call_id={call_id}"
                f"\n  organization_id={resolved_org_id}"
                f"\n  agent_id={resolved_agent_id or 'default_receptionist'}"
                f"\n  caller_phone={resolved_phone}"
                f"\n  caller_name={resolved_name}"
                f"\n  script_id={resolved_script_id or 'none'}"
                f"\n  knowledge_id={resolved_knowledge_id or 'none'}"
            )

            logger.info(
                f"\n[ACTIVE_RAG]"
                f"\n  call_id={call_id}"
                f"\n  organization_id={resolved_org_id}"
                f"\n  agent_id={resolved_agent_id or 'default_receptionist'}"
                f"\n  script_id={resolved_script_id or 'none'}"
                f"\n  knowledge_id={resolved_knowledge_id or 'none'}"
                f"\n  status={'ACTIVE' if resolved_knowledge_id else 'NONE'}"
                f"\n  context_loaded={bool(resolved_context_len > 0)}"
                f"\n  context_length={resolved_context_len}"
                f"\n  source_count={resolved_source_count}"
            )

            def audio_send(pcm_bytes: bytes):
                """Send 8-bit unsigned 8000Hz PCM audio back via pyVoIP RTP."""
                try:
                    if call.state == CallState.ANSWERED:
                        if hasattr(call, "write_audio"):
                            call.write_audio(pcm_bytes)
                        elif hasattr(call, "writeAudio"):
                            call.writeAudio(pcm_bytes)
                except Exception as exc:
                    logger.warning(f"[SIP INBOUND RTP OUT] Send error: {exc}")

            def audio_clear():
                """Flush pyVoIP transmit buffer for instant barge-in."""
                try:
                    if hasattr(call, "RTPClients") and call.RTPClients:
                        for client in call.RTPClients:
                            if hasattr(client, "pmout") and client.pmout:
                                try:
                                    with client.pmout.bufferLock:
                                        import io
                                        client.pmout.buffer = io.BytesIO()
                                        client.pmout.log = {}
                                        client.pmout.offset = getattr(client, "outOffset", 0)
                                except Exception:
                                    pass
                    if hasattr(call, "rtp") and call.rtp:
                        if hasattr(call.rtp, "clear"):
                            call.rtp.clear()
                        elif hasattr(call.rtp, "clear_tx_buffer"):
                            call.rtp.clear_tx_buffer()
                except Exception as exc:
                    logger.warning(f"[SIP INBOUND BARGE-IN CLEAR] Error: {exc}")

            from src.services.sip_call_handler import SipCallSession, CallType
            session = SipCallSession(
                caller_phone=resolved_phone,
                caller_name=resolved_name,
                call_type=CallType.INBOUND,
                call_sid=call_id,
                organization_id=resolved_org_id,
                audio_send_callback=audio_send,
                audio_clear_callback=audio_clear,
                agent_id=resolved_agent_id,
                active_script_id=resolved_script_id,
                active_knowledge_id=resolved_knowledge_id,
            )
            session.call_type = CallType.INBOUND
            session.pyvoip_call = call

            # Immediately notify live dashboard with trusted caller identity
            try:
                SipRegistrar._emit_incoming_call_event(
                    session=session,
                    caller_phone=resolved_phone,
                    caller_name=resolved_name,
                    customer_info=customer_info
                )
            except Exception as _em_err:
                logger.warning(f"[INBOUND EVENT EMIT ERROR] {_em_err}")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def run_session():
                logger.info(f"[SIP INBOUND SESSION] Session {session.call_sid} starting.")
                rtp_task = asyncio.create_task(self._rtp_receive_loop(call, session))
                ai_task = asyncio.create_task(session.process_audio_loop())
                await session.start()

                from pyVoIP.VoIP import CallState
                while not session.ended and getattr(call, "state", None) == CallState.ANSWERED:
                    await asyncio.sleep(0.2)

                logger.info(f"[SIP INBOUND SESSION] Session {session.call_sid} loop ended.")
                from src.services.sip_call_handler import terminate_call
                await terminate_call(session.call_sid, reason=getattr(session, "end_reason", "REMOTE_HANGUP"))

                for task in (rtp_task, ai_task):
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

            loop.run_until_complete(run_session())
            loop.close()

        except Exception as exc:
            logger.error(f"[SIP INBOUND] Call handling error: {exc}")
            try:
                call.hangup()
            except Exception:
                pass

    async def _rtp_receive_loop(self, call, session: "SipCallSession"):
        """Read RTP audio frames from the call and feed to the session."""
        from pyVoIP.VoIP import CallState
        logger.info(f"[SIP RTP IN] Starting RTP receive for {session.call_sid}")
        frames_received = 0
        frames_silent = 0
        FRAME_SIZE = 160

        while not session.ended:
            try:
                if call.state != CallState.ANSWERED:
                    logger.info(f"[SIP_BYE_RECEIVED] call_id={session.call_sid} state={call.state}")
                    session.end_reason = "REMOTE_HANGUP"
                    session.ended = True
                    break

                client = call.RTPClients[0] if getattr(call, "RTPClients", None) else None
                pmin = getattr(client, "pmin", None) if client else None

                packets_drained = 0
                while not session.ended and packets_drained < 10:
                    audio_bytes = None
                    if pmin is not None:
                        unread = 0
                        with pmin.bufferLock:
                            unread = pmin.buffer.getbuffer().nbytes - pmin.buffer.tell()
                        if unread < FRAME_SIZE:
                            break
                        audio_bytes = pmin.read(FRAME_SIZE)
                    elif hasattr(call, "read_audio"):
                        audio_bytes = call.read_audio(FRAME_SIZE, blocking=False)
                    elif hasattr(call, "readAudio"):
                        audio_bytes = call.readAudio(FRAME_SIZE, blocking=False)

                    if not audio_bytes or len(audio_bytes) < FRAME_SIZE:
                        break

                    is_silence = (
                        audio_bytes == b"\x80" * len(audio_bytes) or
                        audio_bytes == b"\x00" * len(audio_bytes)
                    )

                    # If pmin was unavailable and read returned artificial silence, stop draining
                    if pmin is None and is_silence:
                        break

                    packets_drained += 1
                    t_now = time.time()
                    if frames_received + frames_silent == 0:
                        logger.info(f"[INBOUND RTP RECEIVED] First RTP stream packet connected for {session.call_sid}")

                    session.feed_audio(audio_bytes[:FRAME_SIZE])

                    if not is_silence:
                        frames_received += 1
                        if frames_received == 1:
                            logger.info(f"[INBOUND RTP RECEIVED] First real voice frame received for {session.call_sid} ({len(audio_bytes)} bytes)")
                        elif frames_received % 50 == 0:  # log every ~1 second of voice
                            logger.info(f"[SIP RTP IN] {frames_received} real voice frames received")
                        logger.debug(f"[RTP_RECEIVED] call_id={session.call_sid} timestamp={t_now:.4f}")
                    else:
                        frames_silent += 1

                if packets_drained > 0:
                    await asyncio.sleep(0.005)
                else:
                    await asyncio.sleep(0.005)
            except Exception as exc:
                logger.warning(f"[SIP RTP IN] Receive error (retrying): {exc}")
                await asyncio.sleep(0.02)

        logger.info(f"[SIP RTP IN] RTP receive loop ended for {session.call_sid}. "
                    f"Real frames: {frames_received}, Silent frames: {frames_silent}")
        # Do NOT call session.stop() here — terminate_call() handles all cleanup
        # when it was already called inside the loop (REMOTE_HANGUP path)

    def _is_phone_alive(self) -> bool:
        """Check whether the pyVoIP phone is initialized and socket is active."""
        try:
            if self._phone is None:
                return False
            sip_client = getattr(self._phone, "sip", None)
            if sip_client is None:
                return False
            sock = getattr(sip_client, "s", None)
            if sock is None:
                return False
            if getattr(sock, "_closed", False) is True:
                return False
            return True
        except Exception:
            return False

    def _teardown_phone(self):
        """Gracefully stop the pyVoIP phone and close underlying socket handles to prevent port leaks."""
        try:
            phone_to_stop = self._phone
            self._phone = None
            if phone_to_stop:
                try:
                    phone_to_stop._shutting_down = True
                except Exception:
                    pass
                # Set NSD = False first so pyVoIP loops know not to send/recv anymore
                if hasattr(phone_to_stop, "sip") and phone_to_stop.sip:
                    phone_to_stop.sip.NSD = False
                # Graceful stop
                try:
                    phone_to_stop.stop()
                except Exception:
                    pass
                # Ensure all socket handles are closed to free ports immediately
                for component in ("sip", "rtp"):
                    target = getattr(phone_to_stop, component, None)
                    if target:
                        for sock_attr in ("s", "out", "sin", "sout"):
                            sock = getattr(target, sock_attr, None)
                            if sock and hasattr(sock, "close"):
                                try:
                                    sock.close()
                                except Exception:
                                    pass
        except Exception as exc:
            logger.warning(f"[SIP REG] Error tearing down phone: {exc}")

    def _persist_status(self, status: str, detail: str):
        """Persist registration status to the database."""
        try:
            from src.config.database import SessionLocal
            from src.models.sip_config import SipConfig
            from sqlalchemy.sql import func as sqlfunc

            db = SessionLocal()
            try:
                cfg = db.query(SipConfig).filter(SipConfig.organization_id == self.organization_id).first()
                if not cfg:
                    cfg = db.query(SipConfig).filter(
                        (SipConfig.sip_host == self.sip_host) &
                        (SipConfig.sip_username == self.sip_username)
                    ).first()
                if cfg:
                    cfg.registration_status = status
                    cfg.registration_detail = detail
                    if status == "registered":
                        cfg.last_registered_at = sqlfunc.now()
                    db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.warning(f"[SIP REG] Failed to persist status: {exc}")

    @staticmethod
    def _emit_incoming_call_event(session, caller_phone: str, caller_name: str, customer_info: Optional[dict] = None):
        """Emit Socket.IO event to update the live dashboard with resolved customer details."""
        try:
            from src.sockets.socket_manager import sio
            import asyncio as _asyncio

            caller_obj = customer_info or {
                "source": "INBOUND",
                "phone": caller_phone,
                "name": caller_name,
                "contact_id": None,
                "is_known": (caller_name not in ("Unknown", "Unknown Caller", "Customer Mobile", "SIP Caller")),
            }

            logger.info(f"[CALLER_ID] call_id={getattr(session, 'call_sid', '')} caller_number={caller_phone} caller_name={caller_name}")

            call_identifier = getattr(session, "call_sid", "") or str(getattr(session, "db_call_id", ""))
            payload = {
                "call_id": call_identifier,
                "call_sid": call_identifier,
                "id": call_identifier,
                "customer_number": caller_phone,
                "caller_number": caller_phone,
                "caller_phone": caller_phone,
                "customer_name": caller_name,
                "caller_name": caller_name,
                "phone": caller_phone,
                "name": caller_name,
                "from": caller_phone,
                "caller": caller_obj,
                "status": "in-progress",
                "event": "incoming-call",
                "direction": "inbound",
                "source": "sip_trunk",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            logger.info(f"[SIP REG] Emitting auto-answered incoming call to dashboard: {payload}")

            async def _do_emit():
                await sio.emit("incoming-call", payload)
                await sio.emit("incoming_call", payload)
                await sio.emit("call_status_update", {
                    "call_id": call_identifier,
                    "call_sid": call_identifier,
                    "id": call_identifier,
                    "status": "in-progress",
                    "customer_number": caller_phone,
                    "caller_number": caller_phone,
                    "caller_phone": caller_phone,
                    "customer_name": caller_name,
                    "caller_name": caller_name,
                    "phone": caller_phone,
                    "name": caller_name,
                    "caller": caller_obj,
                })

            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(_do_emit())
            except RuntimeError:
                # No running loop in this background thread — run in a new event loop
                new_loop = _asyncio.new_event_loop()
                try:
                    new_loop.run_until_complete(_do_emit())
                finally:
                    new_loop.close()
        except Exception as exc:
            logger.warning(f"[SIP REG] Socket emit error: {exc}")

    @staticmethod
    def _resolve_customer_identity(phone: str, organization_id: int = 1, initial_name: str = "") -> dict:
        """
        Dynamically resolves customer/contact record from the database for the tenant organization.
        Checks:
          1. Appointment table (by matching customer phone number)
          2. Lead table (by matching lead phone number)
          3. CallLog table (by matching caller phone number with verified non-generic name)
        """
        if not phone or phone in ("Unknown", "Unknown Caller", "Private Number"):
            fallback_name = initial_name.strip() if (initial_name and initial_name.strip() not in ("Unknown", "Unknown Caller", "Anonymous", "Customer Mobile", "SIP Caller", "Customer", "Private Number")) else (phone or "Unknown Caller")
            return {
                "phone": phone or "Unknown Caller",
                "name": fallback_name,
                "contact_id": None,
                "is_known": False,
                "email": None,
            }

        digits = "".join(c for c in phone if c.isdigit())
        d10 = digits[-10:] if len(digits) >= 10 else digits

        if not d10:
            fallback_name = initial_name.strip() if (initial_name and initial_name.strip() not in ("Unknown", "Unknown Caller", "Anonymous", "Customer Mobile", "SIP Caller", "Customer", "Private Number")) else (phone or "Unknown Caller")
            return {
                "phone": phone or "Unknown Caller",
                "name": fallback_name,
                "contact_id": None,
                "is_known": False,
                "email": None,
            }

        try:
            from src.config.database import SessionLocal
            from src.models.lead import Lead

            db = SessionLocal()
            try:
                # Check verified CRM Lead table only (never infer caller identity from past appointment records)
                lead = db.query(Lead).filter(
                    Lead.organization_id == organization_id,
                    Lead.phone.like(f"%{d10}")
                ).order_by(Lead.updated_at.desc()).first()

                if lead and lead.name and lead.name.strip() and lead.name != "Unknown Lead":
                    l_name = lead.name.strip()
                    if l_name not in ("Unknown", "Unknown Caller", "Anonymous", "Customer Mobile", "SIP Caller", "Customer", "Physical Mobile Test", "Test Physical Mobile", "Private Number", "Name", "name", "None", "null"):
                        logger.info(f"[CUSTOMER_RESOLVED] Matched Verified Lead: {l_name} ({phone})")
                        return {
                            "phone": lead.phone or phone,
                            "name": l_name,
                            "contact_id": str(lead.id),
                            "is_known": True,
                            "email": None,
                        }
            finally:
                db.close()
        except Exception as err:
            logger.warning(f"[CUSTOMER_RESOLVE_ERROR] {err}")

        final_name = initial_name.strip() if (initial_name and initial_name.strip() not in ("Unknown", "Unknown Caller", "Anonymous", "Customer Mobile", "SIP Caller", "Customer", "Physical Mobile Test", "Test Physical Mobile", "Test Physical Customer", "Private Number", "Name", "name", "None", "null")) else phone
        return {
            "phone": phone,
            "name": final_name,
            "contact_id": None,
            "is_known": (final_name != phone and final_name not in ("Unknown", "Unknown Caller", "Private Number")),
            "email": None,
        }

    @staticmethod
    def _extract_customer_phone_and_name(req_headers: dict, trunk_username: str = "", did_number: str = "", raw_sip: str = "") -> tuple:
        """
        Extract the actual customer's mobile/phone number and caller name from SIP INVITE headers.
        Strictly distinguishes caller number from the destination DID / trunk line number.
        """
        import re
        from src.config.env import settings

        # Build case-insensitive header dictionary
        headers_lower = {}
        if isinstance(req_headers, dict):
            for k, v in req_headers.items():
                if k:
                    headers_lower[str(k).lower().strip()] = v

        # Collect callee / DID / trunk numbers that must NEVER be returned as the customer's phone number
        disallowed_raw = set()
        disallowed_d10 = set()

        def clean_raw_digits(val: Any) -> str:
            if not val:
                return ""
            if isinstance(val, dict):
                # Inspect caller (display name) and number (URI user)
                caller_val = val.get("caller")
                if caller_val and not isinstance(caller_val, (dict, list)):
                    c_res = clean_raw_digits(str(caller_val))
                    if c_res and len(c_res) >= 10:
                        c_d10 = c_res[-10:]
                        if c_d10 not in disallowed_d10 and c_res not in disallowed_raw:
                            return c_res

                num_val = val.get("number")
                if num_val and not isinstance(num_val, (dict, list)):
                    n_res = clean_raw_digits(str(num_val))
                    if n_res and len(n_res) >= 10:
                        return n_res

                for sub_k in ("address", "host", "raw"):
                    v = val.get(sub_k)
                    if v and not isinstance(v, (dict, list)):
                        res = clean_raw_digits(str(v))
                        if res and len(res) >= 10:
                            return res
                return ""
            v_str = str(val).strip().strip('"').strip("'")
            uri_match = re.search(r'(?:sip:|tel:)([^@;>]+)', v_str, re.IGNORECASE)
            target = uri_match.group(1) if uri_match else v_str
            return re.sub(r'\D', '', target)

        def format_phone(digits: str) -> str:
            if not digits:
                return "Unknown Caller"
            digits = re.sub(r'\D', '', str(digits))
            if digits.startswith("91") and len(digits) == 12:
                return f"+{digits}"
            elif len(digits) == 10:
                return f"+91{digits}"
            elif digits.startswith("0") and len(digits) == 11:
                return f"+91{digits[1:]}"
            elif not digits.startswith("+") and len(digits) >= 10:
                return f"+{digits}"
            return digits if digits.startswith("+") else f"+{digits}"

        def is_private_indicator(val: Any) -> bool:
            if not val:
                return False
            s = str(val).lower()
            return any(w in s for w in ("anonymous", "private", "restricted", "withheld", "unavailable", "secret", "hidden"))

        # Check for private / withheld caller IDs
        from_hdr = headers_lower.get("from") or req_headers.get("From") or {}
        privacy_hdr = headers_lower.get("privacy") or req_headers.get("Privacy") or ""
        pai_hdr = headers_lower.get("p-asserted-identity") or req_headers.get("P-Asserted-Identity") or ""
        rpid_hdr = headers_lower.get("remote-party-id") or req_headers.get("Remote-Party-ID") or ""

        # RFC 3323: Privacy header values like 'id', 'user', 'header', 'full' denote caller privacy
        has_privacy_hdr = bool(privacy_hdr and str(privacy_hdr).lower().strip() not in ("none", "off", ""))
        has_rpid_privacy = bool(rpid_hdr and any(p in str(rpid_hdr).lower() for p in ("privacy=full", "privacy=uri", "privacy=name", "privacy=id")))

        if is_private_indicator(from_hdr) or is_private_indicator(privacy_hdr) or is_private_indicator(pai_hdr) or is_private_indicator(rpid_hdr) or has_privacy_hdr or has_rpid_privacy:
            return "Private Number", "Private Number"

        for candidate in (
            did_number,
            trunk_username,
            getattr(settings, "SIP_DID_NUMBER", ""),
            getattr(settings, "SIP_USERNAME", ""),
            getattr(settings, "SIP_ACCOUNT_NAME", ""),
            getattr(settings, "SIP_LOGIN", ""),
            getattr(settings, "SIP_EXTENSION", ""),
        ):
            cd = clean_raw_digits(candidate)
            if cd:
                disallowed_raw.add(cd)
                d10 = cd[-10:] if len(cd) >= 10 else cd
                if d10:
                    disallowed_d10.add(d10)
                    disallowed_raw.add(f"0{d10}")
                    disallowed_raw.add(f"91{d10}")
                    disallowed_raw.add(f"0091{d10}")

        # Dynamically query configured SipConfig trunk & extension numbers
        try:
            from src.config.database import SessionLocal
            from src.models.sip_config import SipConfig
            _db = SessionLocal()
            try:
                for sc in _db.query(SipConfig).all():
                    for field in (sc.did_number, sc.sip_username, sc.account_name, sc.login, sc.sip_extension, sc.sip_server):
                        if field:
                            _cd = clean_raw_digits(field)
                            if _cd:
                                disallowed_raw.add(_cd)
                                _d10 = _cd[-10:] if len(_cd) >= 10 else _cd
                                if _d10:
                                    disallowed_d10.add(_d10)
                                    disallowed_raw.add(f"0{_d10}")
                                    disallowed_raw.add(f"91{_d10}")
            finally:
                _db.close()
        except Exception:
            pass

        # Extract callee numbers from To header to prevent destination DID from being returned
        to_hdr = headers_lower.get("to") or req_headers.get("To") or {}
        to_digits = ""
        if isinstance(to_hdr, dict):
            to_digits = clean_raw_digits(to_hdr.get("number") or to_hdr.get("address") or to_hdr.get("raw"))
        elif isinstance(to_hdr, str):
            to_digits = clean_raw_digits(to_hdr)
        if to_digits:
            disallowed_raw.add(to_digits)
            d10 = to_digits[-10:] if len(to_digits) >= 10 else to_digits
            if d10:
                disallowed_d10.add(d10)
                disallowed_raw.add(f"0{d10}")
                disallowed_raw.add(f"91{d10}")
                disallowed_raw.add(f"0091{d10}")

        # Extract Request-URI number (callee DID) to prevent leaking
        if raw_sip:
            req_line_m = re.search(r'^(?:INVITE|ACK|BYE)\s+sip:(\+?\d+)', raw_sip, re.MULTILINE)
            if req_line_m:
                r_digits = clean_raw_digits(req_line_m.group(1))
                if r_digits:
                    disallowed_raw.add(r_digits)
                    d10 = r_digits[-10:] if len(r_digits) >= 10 else r_digits
                    if d10:
                        disallowed_d10.add(d10)
                        disallowed_raw.add(f"0{d10}")
                        disallowed_raw.add(f"91{d10}")

        def parse_customer_mobile(raw_digits: str) -> str:
            if not raw_digits:
                return ""
            digits = re.sub(r'\D', '', str(raw_digits))
            if not digits:
                return ""

            # Check carrier concatenated strings (e.g. 09842110746 + 917935484931 or 09092649453 + 917935484931)
            if len(digits) >= 18:
                customer_part = ""
                # Priority 1: Strip known trunk DID if it is concatenated at the end or beginning
                for d10 in disallowed_d10:
                    if digits.endswith(d10):
                        prefix = digits[:-len(d10)]
                        if prefix.endswith("91"):
                            prefix = prefix[:-2]
                        if len(prefix) >= 10:
                            customer_part = prefix
                            break
                    elif digits.startswith(d10):
                        suffix = digits[len(d10):]
                        if len(suffix) >= 10:
                            customer_part = suffix
                            break

                if not customer_part:
                    if digits.startswith("0") and len(digits) >= 11 and digits[1] in "56789":
                        if digits[:11][-10:] in disallowed_d10 or digits[:11] in disallowed_raw:
                            customer_part = digits[11:]
                        else:
                            customer_part = digits[:11]
                    elif digits.startswith("91") and len(digits) >= 12 and digits[2] in "56789":
                        if digits[:12][-10:] in disallowed_d10 or digits[:12] in disallowed_raw:
                            customer_part = digits[12:]
                        else:
                            customer_part = digits[:12]
                    elif digits[0] in "56789":
                        if digits[:10] in disallowed_d10 or digits[:10] in disallowed_raw:
                            customer_part = digits[10:]
                        else:
                            customer_part = digits[:10]
                    else:
                        customer_part = digits[:11] if digits.startswith("0") else digits[:10]

                c_d10 = customer_part[-10:] if len(customer_part) >= 10 else customer_part
                if len(customer_part) >= 10 and c_d10 not in disallowed_d10 and customer_part not in disallowed_raw:
                    return format_phone(customer_part)
                return ""

            cand_d10 = digits[-10:] if len(digits) >= 10 else digits
            if cand_d10 in disallowed_d10 or digits in disallowed_raw:
                return ""

            if len(digits) >= 10:
                return format_phone(digits)
            return ""

        def extract_name_from_str(hdr_str: Any) -> str:
            if not hdr_str:
                return ""
            if isinstance(hdr_str, dict):
                caller = hdr_str.get("caller") or hdr_str.get("name") or ""
                if caller and not str(caller).isdigit() and len(str(caller)) < 40 and str(caller) not in ("Unknown", "Anonymous", "<"):
                    return str(caller).strip().strip('"').strip('<').strip()
                hdr_str = hdr_str.get("raw", "")
            cleaned_hdr = re.sub(r'^(?:From|To|Contact|PAI|RPID|P-Asserted-Identity|Remote-Party-ID):\s*', '', str(hdr_str).strip(), flags=re.IGNORECASE)
            m = re.match(r'^"?([^"<]+)"?\s*<', cleaned_hdr.strip())
            if m:
                n = m.group(1).strip().strip('"').strip(':').strip()
                if n and not n.isdigit() and len(n) < 40 and n.lower() not in ("unknown", "anonymous", "from", "to", "contact", "pai", "rpid", "<"):
                    return n
            return ""

        # 1. Check P-Asserted-Identity (PAI) & P-Preferred-Identity
        for pai_key in ("p-asserted-identity", "p-preferred-identity", "pai"):
            pai = headers_lower.get(pai_key)
            if pai:
                pai_name = extract_name_from_str(pai)
                if isinstance(pai, dict):
                    pai = pai.get("raw", str(pai))
                pai_digits = clean_raw_digits(pai)
                cust_phone = parse_customer_mobile(pai_digits)
                if cust_phone and cust_phone not in ("Unknown", "Unknown Caller", "Private Number"):
                    return cust_phone, pai_name

        # 2. Check Remote-Party-ID (RPID)
        for rpid_key in ("remote-party-id", "rpid"):
            rpid = headers_lower.get(rpid_key)
            if rpid:
                rpid_name = extract_name_from_str(rpid)
                if isinstance(rpid, dict):
                    rpid = rpid.get("raw", str(rpid))
                rpid_digits = clean_raw_digits(rpid)
                cust_phone = parse_customer_mobile(rpid_digits)
                if cust_phone and cust_phone not in ("Unknown", "Unknown Caller", "Private Number"):
                    return cust_phone, rpid_name

        # 3. Check X-Caller-ID / X-Real-Caller / X-Customer-Phone / Diversion
        for x_hdr in ("x-caller-id", "x-real-caller", "x-customer-phone", "x-original-caller-id", "diversion"):
            val = headers_lower.get(x_hdr)
            if val:
                x_name = extract_name_from_str(val)
                d = clean_raw_digits(val)
                cust_phone = parse_customer_mobile(d)
                if cust_phone and cust_phone not in ("Unknown", "Unknown Caller", "Private Number"):
                    return cust_phone, x_name

        # 4. Check From header
        from_caller = ""
        from_num = ""

        if isinstance(from_hdr, dict):
            raw_caller = str(from_hdr.get("caller") or from_hdr.get("name") or from_hdr.get("display") or "").strip().strip('"').strip("'")
            if raw_caller and raw_caller != "<":
                from_caller = raw_caller
            from_num = str(from_hdr.get("number") or from_hdr.get("address") or from_hdr.get("host") or from_hdr.get("raw") or "").strip()
        elif hasattr(from_hdr, "number") or hasattr(from_hdr, "address") or hasattr(from_hdr, "raw"):
            raw_caller = str(getattr(from_hdr, "caller", None) or getattr(from_hdr, "name", None) or getattr(from_hdr, "display", None) or "").strip().strip('"').strip("'")
            if raw_caller and raw_caller != "<":
                from_caller = raw_caller
            from_num = str(getattr(from_hdr, "number", None) or getattr(from_hdr, "address", None) or getattr(from_hdr, "raw", None) or "").strip()
        elif isinstance(from_hdr, str):
            raw_from = str(from_hdr or "")
            m_disp = re.match(r'^"?([^"<]+)"?\s*<sip:([^@>]+)', raw_from)
            if m_disp:
                from_caller = m_disp.group(1).strip()
                from_num = m_disp.group(2).strip()
            else:
                m_uri = re.search(r'<sip:([^@>]+)', raw_from) or re.search(r'sip:([^@>]+)', raw_from) or re.search(r'<tel:([^@>]+)', raw_from)
                if m_uri:
                    from_num = m_uri.group(1).strip()
                else:
                    from_num = raw_from.strip()

        caller_digits = clean_raw_digits(from_caller)
        num_digits = clean_raw_digits(from_num)

        # Check caller display name for embedded phone number
        if caller_digits and len(caller_digits) >= 10:
            cust_phone = parse_customer_mobile(caller_digits)
            if cust_phone and cust_phone not in ("Unknown", "Unknown Caller", "Private Number"):
                disp_name = from_caller if (from_caller and not from_caller.isdigit() and len(from_caller) < 30 and from_caller not in ("Unknown", "Anonymous", "<")) else ""
                return cust_phone, disp_name

        # Check From URI number (handles customer number or concatenated string e.g. 09092649453917935484931)
        if num_digits:
            cust_phone = parse_customer_mobile(num_digits)
            if cust_phone and cust_phone not in ("Unknown", "Unknown Caller", "Private Number"):
                disp_name = from_caller if (from_caller and not from_caller.isdigit() and len(from_caller) < 30 and from_caller not in ("Unknown", "Anonymous", "<")) else ""
                return cust_phone, disp_name

        # Fallback: check raw_sip for From header if req_headers was incomplete
        if raw_sip:
            raw_from_m = re.search(r'^(?:From|f):\s*([^\r\n]+)', raw_sip, re.MULTILINE | re.IGNORECASE)
            if raw_from_m:
                line_val = raw_from_m.group(1).strip()
                m_uri = re.search(r'(?:sip:|tel:)([^@;>]+)', line_val, re.IGNORECASE)
                if m_uri:
                    target_num = m_uri.group(1).strip()
                    raw_num_d = clean_raw_digits(target_num)
                    if raw_num_d:
                        p = parse_customer_mobile(raw_num_d)
                        if p and p not in ("Unknown", "Unknown Caller", "Private Number"):
                            return p, extract_name_from_str(line_val)

        # CRITICAL: Strictly never use Contact header, To header, or arbitrary raw SIP lines as caller ID
        return "Unknown Caller", "Unknown Caller"

    @staticmethod
    def _extract_phone(from_header) -> str:
        """Extract phone number from SIP From header (can be str or dict)."""
        import re
        if isinstance(from_header, dict):
            num = from_header.get("number")
            if num:
                return str(num)
            from_header = from_header.get("raw", "")
        if not isinstance(from_header, str):
            from_header = str(from_header or "")
        match = re.search(r"sip:([+\d]+)@", from_header)
        if match:
            return match.group(1)
        match = re.search(r"<sip:(.+?)>", from_header)
        if match:
            return match.group(1).split("@")[0]
        return "Unknown"

    @staticmethod
    def _extract_name(from_header) -> str:
        """Extract display name from SIP From header (can be str or dict)."""
        import re
        if isinstance(from_header, dict):
            caller = from_header.get("caller")
            if caller:
                return str(caller).strip('"').strip("'")
            from_header = from_header.get("raw", "")
        if not isinstance(from_header, str):
            from_header = str(from_header or "")
        match = re.match(r'^"?([^"<]+)"?\s*<', from_header)
        if match:
            name = match.group(1).strip().strip('"')
            return name if name else ""
        return ""


# ─────────────────────────────────────────────────────────────
# Module-level helpers called from telephony service / app startup
# ─────────────────────────────────────────────────────────────

def start_registrar(
    sip_host: str,
    sip_username: str,
    sip_password: str,
    sip_port: int = 5060,
    did_number: Optional[str] = None,
    organization_id: int = 1,
) -> SipRegistrar:
    """
    Create and start a SIP registrar for a specific tenant organization.
    If the tenant already has an active registrar matching the configuration,
    it reuses the existing active registration without creating duplicate threads.
    """
    existing = get_registrar(organization_id=organization_id)
    if existing and existing.is_running() and existing.matches_config(sip_host, sip_username, sip_port, did_number):
        logger.info(f"[SIP REG] Org {organization_id} registrar is already running with matching config. Reusing existing instance.")
        return existing

    # Tear down previous registrar for this organization ONLY
    stop_registrar(organization_id=organization_id)

    reg = SipRegistrar(
        sip_host=sip_host,
        sip_username=sip_username,
        sip_password=sip_password,
        sip_port=sip_port,
        did_number=did_number,
        organization_id=organization_id,
    )
    set_registrar(reg, organization_id=organization_id)
    reg.start()
    return reg


def stop_registrar(organization_id: Optional[int] = None):
    """
    Stop registrar(s).
    If organization_id is provided, stops only that organization's registrar.
    If organization_id is None, stops ALL active registrars (e.g. on application shutdown).
    """
    import threading
    if organization_id is not None:
        existing = get_registrar(organization_id=organization_id)
        if existing:
            set_registrar(None, organization_id=organization_id)
            try:
                threading.Thread(target=existing.stop, daemon=True, name=f"sip-stop-org{organization_id}").start()
            except Exception as exc:
                logger.warning(f"[SIP REG] Error stopping registrar for org {organization_id}: {exc}")
    else:
        all_regs = get_all_registrars()
        for org_id, reg in all_regs.items():
            set_registrar(None, organization_id=org_id)
            try:
                threading.Thread(target=reg.stop, daemon=True, name=f"sip-stop-org{org_id}").start()
            except Exception as exc:
                logger.warning(f"[SIP REG] Error stopping registrar for org {org_id}: {exc}")


def get_registration_status(organization_id: Optional[int] = None) -> Dict[str, Any]:
    """Return registration status dict for a specific organization or default."""
    reg = get_registrar(organization_id=organization_id)
    if reg:
        return reg.get_status()
    return {
        "status": "unregistered",
        "detail": "No SIP registrar active",
        "organization_id": organization_id,
        "sip_host": None,
        "sip_username": None,
        "sip_port": 5060,
        "did_number": None,
        "registered_at": None,
        "last_attempt_at": None,
        "error_message": None,
    }
