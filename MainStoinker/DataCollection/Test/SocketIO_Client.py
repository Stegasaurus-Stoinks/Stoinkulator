import socketio
import Start_config as config
import json
import simplejson
import algotesty
from MainStoinker.NeatTools.decorators import singleton

@singleton
class FrontEndClient:

    

    def __init__(self):
        print("initializing socket connection")
        self.sio = socketio.Client()
        
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
            tickerfulldata = []
            algofulldata = []

            for i in range(len(config.algos)):
                Fulldata = config.algos[i].updatefrontendfulldata()
                algofulldata.append(Fulldata)

            print(algofulldata)

            for i in range(len(config.tickers)):
                Fulldata = self.getDataJson(i)
                tickerfulldata.append({'ticker': config.tickers[i].name, 'data':Fulldata})
                # try:
                #     self.socket.emit('data_send', {'ticker': config.tickers[i], 'data':Fulldata})
                # except Exception as e: 
                #     print(e)
            # print(tickerfulldata)
            print("Sending Fulldata")
            try: 
                # self.socket.emit('update_send',{)
                payload = {"tickerdata":tickerfulldata, "algodata":algofulldata}
                payload = simplejson.dumps(payload, ignore_nan=True)
                # print(payload)
                self.sio.emit('data_send',payload)
                print("full data send")
            except Exception as e: 
                print(e)
        
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

        


    #EMITS BEING USED:
    # data_send
    # update_send
    # config_send
    def emit_data(self, message, payload):
        try: 
            payload = simplejson.dumps(payload, ignore_nan=True)
            self.sio.emit(message, payload)  
        except Exception as e: 
            print(e)



    def getDataJson(self, index):
        result = config.tickers[index].data.to_json(orient="records")
        # print(result)
        return(result)