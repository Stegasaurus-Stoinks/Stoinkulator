import socketio
import threading
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

from livedatacollect4 import IBapi

from datetime import datetime

import threading
import time
import keyboard
import os
import Start_config

tickers = ["AAPL"]#,"GME","MSFT"]
count = 0


# asyncio
sio = socketio.Client()

@sio.on('message2')
def on_message(data):
    # print('I received a message!')
    print(data)
    sio.emit('data_send', {'foo': 'bar'})
    # print("Emitted Data :D")

@sio.on('start_update')
def on_message(data):
    Start_config.updating = 1

@sio.on('stop_update')
def on_message(data):
    Start_config.updating = 0
    

@sio.on('req_data')
def on_message(data):
    Fulldata = app.getDataJson(index = 0)
    # sendData = { "Date":str(dataPoint['Date']), "Open":str(dataPoint['Open']),"High":str(dataPoint['High']),"Low":str(dataPoint['Low']),"Close":str(dataPoint['Close']),"Volume":str(dataPoint['Volume'])}
    # print(Fulldata)

    sio.emit('data_send', Fulldata)

@sio.event
def connect():
    print("I'm connected!")
    
    # sio.emit('data_send', {'foo': 'bar'})

@sio.event
def connect_error(data):
    print("The connection failed!")

@sio.event
def disconnect():
    print("I'm disconnected!")

def connectwebsocket():
    try:
        sio.connect('http://192.168.0.173:3000')

    except:
        print("Connecting to Front End (Socketio) failed")

wst = threading.Thread(target=connectwebsocket)
wst.daemon = True
wst.start()



def run_loop():
	app.run()

app = IBapi()
app.connect('127.0.0.1', 7497, 123)

time.sleep(1)
# app.run()

#Start the socket in a thread
api_thread = threading.Thread(target=run_loop,daemon=True)
api_thread.start()

app.startData(sio,tickers,[],1,0,8) # Backtesting


print("___________________________________________________________")
print("--------------Press 'CTRL' to Close Program----------------")
print("___________________________________________________________")

keyboard.wait('Ctrl')

print("___________________________________________________________")
print("------------------Closing Program...-----------------------")
print("___________________________________________________________")

app.disconnect()

time.sleep(2)

print("Data Collection Closed")