import Start_config

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

import threading
import time
import keyboard
import os
from livedatacollect3 import IBapi
from EMACrossing_Algo import Algo as EMAAlgo


import socketio
import threading
from time import sleep

count = 0

def test():
    global count
    count +=1
    print(count)


# asyncio
sio = socketio.Client()

@sio.on('message')
def on_message(data):
    # print('I received a message!')
    print(data)
    sio.emit('data_send', {'foo': 'bar'})
    # print("Emitted Data :D")
    test()

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
# app.run()

#Start the socket in a thread
api_thread = threading.Thread(target=run_loop,daemon=True)
api_thread.start()

time.sleep(1) #Sleep interval to allow time for connection to server

tickers = ["AAPL"]#,"GME","MSFT"]

Algo1EMA = EMAAlgo('AAPL', 20, 50, printInfo = False)

Algos = [Algo1EMA]

# app.startData(ticker_list(list),warmup_period(int days),Live Data Yes/No, Backtesting Duration)
app.startData(tickers,Algos,1,Start_config.LiveData,8) # Backtesting



# ^^^ All Code Stuff Goes Above This Line ^^^

print("___________________________________________________________")
print("--------------Press 'CTRL' to Close Program----------------")
print("___________________________________________________________")

keyboard.wait('Ctrl')

print("___________________________________________________________")
print("------------------Closing Program...-----------------------")
print("___________________________________________________________")
# app.printAlgoStats(False)
app.disconnect()
print(count)
mydir = os.path.join(os.path.dirname(__file__), './Collected_Data/')
for f in os.listdir(mydir):
    os.remove(os.path.join(mydir, f))