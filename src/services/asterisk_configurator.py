"""
Asterisk Dynamic Configuration Generator for Multi-Tenant AI Voice Receptionist.

Automatically generates PJSIP endpoints, auth digest credentials, AORs,
and dialplan routes dynamically whenever frontend users submit SIP trunk configuration.
"""
import os
import sys
import logging
import subprocess
from typing import Optional, Dict, Any

logger = logging.getLogger("ai-receptionist.asterisk_configurator")


class AsteriskConfigurator:
    """Manages dynamic Asterisk PJSIP configuration and dialplan updates."""

    @staticmethod
    def _is_wsl_or_linux() -> bool:
        """Check if Linux or WSL is available."""
        if sys.platform != "win32":
            return True
        try:
            res = subprocess.run(["wsl.exe", "--", "which", "asterisk"], capture_output=True, text=True, timeout=2)
            return res.returncode == 0
        except Exception:
            return False

    @classmethod
    def sync_sip_config(
        cls,
        org_id: int,
        user_id: int,
        sip_host: str,
        sip_username: str,
        sip_password: str,
        sip_port: int = 5060,
        did_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Dynamically generate PJSIP endpoint and dialplan configuration for Asterisk.
        Enforces multi-tenant isolation using org_{org_id} context names.
        """
        sip_host = (sip_host or "127.0.0.1").strip()
        sip_username = (sip_username or f"agent_{org_id}").strip()
        sip_port = sip_port or 5060
        endpoint_name = f"org_{org_id}_ep"
        auth_name = f"auth_org_{org_id}"
        aor_name = f"aor_org_{org_id}"
        context_name = f"context_org_{org_id}"

        # 1. PJSIP Configuration Block
        pjsip_content = f"""; =====================================================================
; Dynamically Generated PJSIP Configuration for Org #{org_id}
; =====================================================================

[global]
type=global
user_agent=Asterisk-AI-Receptionist

[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060

[{endpoint_name}]
type=endpoint
context={context_name}
disallow=all
allow=ulaw,alaw,opus,gsm
auth={auth_name}
aors={aor_name}
direct_media=no

[{auth_name}]
type=auth
auth_type=userpass
username={sip_username}
password={sip_password}

[{aor_name}]
type=aor
max_contacts=10
remove_existing=yes
"""

        # 2. Dialplan Configuration Block
        did_pattern = did_number.strip() if did_number and did_number.strip() else "_X."
        dialplan_content = f"""; =====================================================================
; Dynamically Generated Dialplan for Org #{org_id}
; =====================================================================

[general]
static=yes
writeprotect=no

[{context_name}]
exten => {sip_username},1,NoOp(=== Inbound Call to Org #{org_id} AI Receptionist ===)
 same => n,Answer()
 same => n,System(curl -s -X POST http://127.0.0.1:8000/api/v1/telephony/sip/inbound -H "Content-Type: application/json" -d '{{"call_sid":"sip_${{UNIQUEID}}","from_number":"${{CALLERID(num)}}","org_id":{org_id}}}')
 same => n,Hangup()

exten => {did_pattern},1,NoOp(=== Inbound DID Call to Org #{org_id} ===)
 same => n,Answer()
 same => n,System(curl -s -X POST http://127.0.0.1:8000/api/v1/telephony/sip/inbound -H "Content-Type: application/json" -d '{{"call_sid":"sip_${{UNIQUEID}}","from_number":"${{CALLERID(num)}}","org_id":{org_id}}}')
 same => n,Hangup()
"""

        # Write to Linux/WSL Asterisk configuration
        try:
            if sys.platform == "win32":
                import base64
                pjsip_b64 = base64.b64encode(pjsip_content.encode('utf-8')).decode('ascii')
                dialplan_b64 = base64.b64encode(dialplan_content.encode('utf-8')).decode('ascii')

                cmd_pjsip = f"echo '{pjsip_b64}' | base64 -d > /etc/asterisk/pjsip.conf"
                cmd_dialplan = f"echo '{dialplan_b64}' | base64 -d > /etc/asterisk/extensions.conf"

                subprocess.run(["wsl.exe", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c", cmd_pjsip], check=True, timeout=1)
                subprocess.run(["wsl.exe", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-c", cmd_dialplan], check=True, timeout=1)

                subprocess.run(["wsl.exe", "-d", "Ubuntu-24.04", "-u", "root", "--", "asterisk", "-rx", "module reload res_pjsip.so"], check=True, timeout=1)
                subprocess.run(["wsl.exe", "-d", "Ubuntu-24.04", "-u", "root", "--", "asterisk", "-rx", "dialplan reload"], check=True, timeout=1)
            else:
                os.makedirs("/etc/asterisk/pjsip.d", exist_ok=True)
                with open(f"/etc/asterisk/pjsip.d/org_{org_id}.conf", "w") as f:
                    f.write(pjsip_content)
                subprocess.run(["asterisk", "-rx", "module reload res_pjsip.so"], timeout=5)

            logger.info(f"[AsteriskConfigurator] Dynamically synchronized PJSIP endpoint '{endpoint_name}' for org #{org_id}")
            return {
                "success": True,
                "endpoint": endpoint_name,
                "context": context_name,
                "message": f"Asterisk PJSIP configuration synchronized for Org #{org_id}."
            }
        except Exception as err:
            logger.warning(f"[AsteriskConfigurator] Asterisk config sync error: {err}")
            return {
                "success": False,
                "error": str(err),
                "message": f"Could not sync Asterisk files: {err}"
            }
