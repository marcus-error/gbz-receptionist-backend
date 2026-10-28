import socketio
import time

sio = socketio.Client()
received = []

@sio.on('incoming_call')
def on_incoming(data):
    received.append(('incoming_call', data))

@sio.on('call_status_update')
def on_status(data):
    received.append(('call_status_update', data))

try:
    sio.connect('http://localhost:8000', transports=['websocket', 'polling'])
    time.sleep(0.5)

    from unittest.mock import MagicMock
    from src.services.sip_registrar import SipRegistrar

    # Test Customer 1: +917935484931
    session1 = MagicMock()
    session1.call_sid = 'test-call-cust-1'
    SipRegistrar._emit_incoming_call_event(session1, '+917935484931', '+917935484931')
    time.sleep(0.5)

    # Test Customer 2: +919876543210
    session2 = MagicMock()
    session2.call_sid = 'test-call-cust-2'
    SipRegistrar._emit_incoming_call_event(session2, '+919876543210', 'Alice')
    time.sleep(0.5)

    sio.disconnect()
    for ev, d in received:
        print(f"EVENT {ev}: call_id={d.get('call_id')} caller_phone={d.get('caller_phone')} cust_num={d.get('customer_number')} caller_name={d.get('caller_name')}")
except Exception as e:
    print('Error:', e)
