import asyncio
import socketio

sio = socketio.AsyncClient()
received = []

@sio.on('incoming_call')
async def on_incoming(data):
    received.append(('incoming_call', data))

@sio.on('call_status_update')
async def on_status(data):
    received.append(('call_status_update', data))

async def main():
    await sio.connect('http://127.0.0.1:8000', transports=['polling', 'websocket'])
    print("Connected to server successfully!")
    await asyncio.sleep(0.5)

    from unittest.mock import MagicMock
    from src.services.sip_registrar import SipRegistrar

    # Test Customer 1: +917935484931
    session1 = MagicMock()
    session1.call_sid = 'test-call-cust-1'
    SipRegistrar._emit_incoming_call_event(session1, '+917935484931', '+917935484931')
    await asyncio.sleep(1.0)

    # Test Customer 2: +919876543210
    session2 = MagicMock()
    session2.call_sid = 'test-call-cust-2'
    SipRegistrar._emit_incoming_call_event(session2, '+919876543210', 'Alice')
    await asyncio.sleep(1.0)

    await sio.disconnect()
    for ev, d in received:
        print(f"EVENT {ev}: call_id={d.get('call_id')} caller_phone={d.get('caller_phone')} cust_num={d.get('customer_number')} caller_name={d.get('caller_name')}")

if __name__ == '__main__':
    asyncio.run(main())
