import socketio
import Start_config as config
import json
import simplejson
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
            self.ConfigSend()
            
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

            print("Sending Fulldata")

            payload = {"tickerdata":tickerfulldata, "algodata":algofulldata}
            self.emit_data("data_send", payload)
            print("full data send")
        
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
    def emit_data(self, message:str, payload):
        try: 
            payload = simplejson.dumps(payload, ignore_nan=True)
            self.sio.emit(message, payload)  
        except Exception as e: 
            print(e)



    def getDataJson(self, index:int):
        result = config.tickers[index].data.to_json(orient="records")
        # print(result)
        return(result)
    


    def send_full_data(self, index:int):
        data = self.getDataJson(index)
        payload = {"tickerdata":data}
        self.emit_data("data_send", payload)
    
    def updateData(self, index:int):
        data = self.getDataJson(index)
        payload = {"tickerdata":data}
        self.emit_data("data_send", payload)

    
    def ConfigSend(self):
        file = open('./MainStoinker/DataCollection/Test/Algo_config.json')

        try:
            parsed_json = json.load(file)
        except Exception as e:
            print("Got the following exception: " + str(e))

        file.close()
        print("Sending Algo Config")
        print(parsed_json)
        try:
            self.sio.emit('config_send', parsed_json)
        except:
            print("Cant send Config, /Not connected to front end")