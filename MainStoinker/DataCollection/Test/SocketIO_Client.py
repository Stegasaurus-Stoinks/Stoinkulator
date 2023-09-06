import socketio
import Start_config as config
import json
import algotesty

class FrontEndClient:

    sio = socketio.Client()

    def __init__(self, IBapp):
        self.app = IBapp
        
    def connectwebsocket(self):
        try:
            self.call_backs()
            self.sio.connect('http://'+config.FrontEndPort)


        except:
            print("Connecting to Front End (Socketio) failed")

    def call_backs(self):
        @self.sio.on('message2')
        def on_message(data):
            # print('I received a message!')
            print(data)
            self.sio.emit('data_send', {'foo': 'bar'})
            # print("Emitted Data :D")

        @self.sio.on('start_update')
        def on_message(data):
            config.updating = 1

        @self.sio.on('stop_update')
        def on_message(data):
            config.updating = 0
        
        @self.sio.on('req_config')
        def on_message(data):
            algotesty.ConfigSend(self.sio)
            
        @self.sio.on('req_data')
        def on_message(data):
            for i in range(len(config.tickers)):
                Fulldata = self.app.getDataJson(index = i)
                # sendData = { "Date":str(dataPoint['Date']), "Open":str(dataPoint['Open']),"High":str(dataPoint['High']),"Low":str(dataPoint['Low']),"Close":str(dataPoint['Close']),"Volume":str(dataPoint['Volume'])}
                # print(Fulldata)
                print("data requested, sending Fulldata")
                self.sio.emit('data_send', {'ticker': config.tickers[i], 'data':Fulldata})

        
        @self.sio.event
        def connect():
            print("I'm connected!")
            
            # sio.emit('data_send', {'foo': 'bar'})

        @self.sio.event
        def connect_error(data):
            print("The connection failed!")

        @self.sio.event
        def disconnect():
            print("I'm disconnected!")

    