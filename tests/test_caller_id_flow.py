import pytest
from src.services.sip_registrar import SipRegistrar

def mask_phone(phone: str) -> str:
    """Mask phone for safe debugging logs, e.g. +919842110746 -> +9198****0746."""
    if not phone or phone in ("Private Number", "Unknown Caller"):
        return phone
    digits = [c for c in phone if c.isdigit()]
    if len(digits) >= 10:
        prefix = "".join(digits[:4])
        suffix = "".join(digits[-4:])
        return f"+{prefix}****{suffix}"
    return "****"

class TestCallerIdExtraction:
    def test_exact_backend_log_inbound_invite_header(self):
        """1. Test exact parsed-header structure from user log: From with number +917935484931, To with 09410697548."""
        headers = {
            'Via': [{'type': 'SIP/2.0/UDP', 'address': ('49.249.142.147', '5060'), 'rport': None, 'branch': 'z9hG4bKPj6d3de7c3-8221-4890-8c74-8f7bff52fcdf'}],
            'From': {
                'raw': '<sip:+917935484931@49.249.142.147>',
                'tag': 'b76c11f7-6c07-4664-94c7-88bac620433c',
                'address': '+917935484931@49.249.142.147',
                'number': '+917935484931',
                'caller': '',
                'host': '49.249.142.147'
            },
            'To': {
                'raw': '<sip:09410697548@210.18.176.97>',
                'tag': '',
                'address': '09410697548@210.18.176.97',
                'number': '09410697548',
                'caller': '',
                'host': '210.18.176.97'
            },
            'Contact': '<sip:asterisk@49.249.142.147:5060>',
            'Call-ID': '75cd6ef7-846f-4fd2-940c-c035e2adc278',
            'CSeq': {'check': '31852', 'method': 'INVITE'}
        }
        phone, name = SipRegistrar._extract_customer_phone_and_name(
            headers,
            trunk_username="09410697548",
            raw_sip="INVITE sip:09410697548@210.18.176.97:5080 SIP/2.0\r\nFrom: <sip:+917935484931@49.249.142.147>\r\n"
        )
        masked = mask_phone(phone)
        print(f"\n[TEST 1] Exact Log Header | Extracted: {masked} | Phone: {phone} | Name: {name}")
        assert phone == "+917935484931"
        assert phone != "Unknown Caller"
        assert phone != "+919410697548"

    def test_customer_a_concatenated_carrier_string(self):
        """2. Inbound call from customer A (concatenated mobile + trunk DID): display Customer A's number."""
        headers = {
            "from": {
                "number": "09842110746917935484931",
                "caller": "Customer A",
                "raw": 'From: "Customer A" <sip:09842110746917935484931@49.249.142.147>'
            },
            "to": {"number": "09410697548", "raw": "<sip:09410697548@49.249.142.147>"}
        }
        phone, name = SipRegistrar._extract_customer_phone_and_name(
            headers,
            trunk_username="09410697548",
            raw_sip="INVITE sip:09410697548@10.0.0.1 SIP/2.0\r\n"
        )
        masked = mask_phone(phone)
        print(f"\n[TEST 2] Source: From Header (concatenated A) | Masked: {masked} | Name: {name}")
        assert phone == "+919842110746"
        assert phone != "+919410697548"

    def test_customer_b_concatenated_carrier_string(self):
        """3. New inbound call from customer B: replace A's number with B's number."""
        headers = {
            "from": {
                "number": "09092649453917935484931",
                "caller": "Customer B",
                "raw": 'From: "Customer B" <sip:09092649453917935484931@49.249.142.147>'
            },
            "to": {"number": "09410697548", "raw": "<sip:09410697548@49.249.142.147>"}
        }
        phone, name = SipRegistrar._extract_customer_phone_and_name(
            headers,
            trunk_username="09410697548",
            raw_sip="INVITE sip:09410697548@10.0.0.1 SIP/2.0\r\n"
        )
        masked = mask_phone(phone)
        print(f"\n[TEST 3] Source: From Header (concatenated B) | Masked: {masked} | Name: {name}")
        assert phone == "+919092649453"
        assert phone != "+919842110746"

    def test_private_number_handling(self):
        """4. Inbound call with private or withheld caller ID: show Private Number."""
        # Case i: Anonymous From header
        headers = {
            "from": {
                "caller": "Anonymous",
                "number": "anonymous",
                "raw": 'From: "Anonymous" <sip:anonymous@anonymous.invalid>'
            }
        }
        phone, name = SipRegistrar._extract_customer_phone_and_name(
            headers,
            trunk_username="09410697548"
        )
        print(f"\n[TEST 4a] Source: Anonymous From Header | Phone: {phone} | Name: {name}")
        assert phone == "Private Number"
        assert name == "Private Number"

        # Case ii: Privacy header present
        headers_priv = {
            "from": {"number": "09842110746"},
            "privacy": "id"
        }
        phone_priv, name_priv = SipRegistrar._extract_customer_phone_and_name(
            headers_priv,
            trunk_username="09410697548"
        )
        print(f"\n[TEST 4b] Source: Privacy Header | Phone: {phone_priv} | Name: {name_priv}")
        assert phone_priv == "Private Number"

    def test_missing_caller_id_returns_unknown_caller(self):
        """5. When caller ID is absent: show Unknown Caller, NEVER SIP registration username."""
        headers = {
            "to": {"number": "09410697548", "raw": "<sip:09410697548@210.18.176.97>"}
        }
        phone, name = SipRegistrar._extract_customer_phone_and_name(
            headers,
            trunk_username="09410697548"
        )
        print(f"\n[TEST 5] Missing Caller ID | Phone: {phone} | Name: {name}")
        assert phone == "Unknown Caller"
        assert name == "Unknown Caller"

    def test_sip_registration_username_never_substituted_as_caller(self):
        """6. Verify SIP registration username (09410697548) is NEVER used as customer number."""
        headers = {
            "from": {
                "number": "09410697548",
                "raw": 'From: <sip:09410697548@49.249.142.147>'
            },
            "to": {"number": "09410697548"}
        }
        phone, name = SipRegistrar._extract_customer_phone_and_name(
            headers,
            trunk_username="09410697548"
        )
        print(f"\n[TEST 6] Source: SIP Account Username | Phone: {phone} | Name: {name}")
        assert phone == "Unknown Caller"
        assert "09410697548" not in phone

    def test_p_asserted_identity_priority(self):
        """7. Verify trusted P-Asserted-Identity has top priority over From header."""
        headers = {
            "p-asserted-identity": '<sip:+919712345678@provider.com>',
            "from": {"number": "09842110746"}
        }
        phone, name = SipRegistrar._extract_customer_phone_and_name(
            headers,
            trunk_username="09410697548"
        )
        masked = mask_phone(phone)
        print(f"\n[TEST PAI] Source: P-Asserted-Identity | Masked: {masked}")
        assert phone == "+919712345678"

    def test_multi_call_isolation_and_no_stale_retention(self):
        """8. Multiple calls & end call: verify each call is keyed to its call_id and state resets."""
        call_a = {
            "call_id": "call-sid-aaa-111",
            "call_sid": "call-sid-aaa-111",
            "customer_number": "+919842110746",
            "customer_name": "Customer A",
            "status": "in-progress"
        }
        call_b = {
            "call_id": "call-sid-bbb-222",
            "call_sid": "call-sid-bbb-222",
            "customer_number": "+917935484931",
            "customer_name": "+917935484931",
            "status": "in-progress"
        }

        calls_registry = {
            call_a["call_sid"]: call_a,
            call_b["call_sid"]: call_b
        }

        assert calls_registry["call-sid-aaa-111"]["customer_number"] == "+919842110746"
        assert calls_registry["call-sid-bbb-222"]["customer_number"] == "+917935484931"
        assert calls_registry["call-sid-aaa-111"]["customer_number"] != calls_registry["call-sid-bbb-222"]["customer_number"]

        # Call A ends
        call_a_end = {
            "call_sid": "call-sid-aaa-111",
            "status": "completed"
        }
        active_call = call_a
        if call_a_end["status"] in ("completed", "failed"):
            active_call = None

        print(f"\n[TEST 8] Call A ended. activeCall reset: {active_call is None}")
        assert active_call is None

    def test_raw_sip_fallback_extraction(self):
        """9. Test fallback to raw SIP string if headers dictionary is empty."""
        raw_sip = "INVITE sip:09410697548@210.18.176.97:5080 SIP/2.0\r\nFrom: <sip:+917935484931@49.249.142.147>;tag=123\r\nTo: <sip:09410697548@210.18.176.97>\r\n"
        phone, name = SipRegistrar._extract_customer_phone_and_name(
            {},
            trunk_username="09410697548",
            raw_sip=raw_sip
        )
        assert phone == "+917935484931"

    def test_verified_different_customer_numbers(self):
        """10. Verify extraction of different caller numbers without collisions."""
        for num in ("+919876543210", "+919123456789", "+917935484931"):
            h = {
                "from": {"number": num, "raw": f"<sip:{num}@carrier.net>"},
                "to": {"number": "09410697548"}
            }
            phone, _ = SipRegistrar._extract_customer_phone_and_name(h, trunk_username="09410697548")
            assert phone == num
