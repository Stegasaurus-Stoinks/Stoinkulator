import socketio
import threading
from time import sleep

# asyncio
sio = socketio.Client()

@sio.event
def message(data):
    print('I received a message!')

@sio.on('message')
def on_message(data):
    print('I received a message!')
    print(data)

@sio.on('*')
async def catch_all(event, data):
   pass

@sio.event
def connect():
    print("I'm connected!")

@sio.event
def connect_error(data):
    print("The connection failed!")

@sio.event
def disconnect():
    print("I'm disconnected!")

def connectwebsocket():
    sio.connect('http://192.168.1.39:3000')

wst = threading.Thread(target=connectwebsocket)
wst.daemon = True
wst.start()

while(1):
    sleep(1)
