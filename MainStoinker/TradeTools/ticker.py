import pandas as pd
import time
import MainStoinker.MainStuff.Start_config as config
from MainStoinker.Util.SocketIO_Client import FrontEndClient as sio



class Ticker():

    def __init__(self, name, index):
        self.name = name
        self.index = index
        self.data = pd.DataFrame([], columns = ['date','time','open','high','low','close','volume'])
        # self.data.columns = ['date','time','open','high','low','close','volume']
        self.registeredAlgos = []
        self.socket = sio()

    
    def register_algo(self, algo):
        if not algo in self.registeredAlgos:
            self.registeredAlgos.append(algo)


    def unregister_algo(self, algo):
       if algo in self.registeredAlgos:
            self.registeredAlgos.remove(algo)


    def update_algos(self):
        algodata = []
        
        # update all associated algos
        for algo in self.registeredAlgos:
            if config.LiveData:
                # print(self.data)
                # print(self.data.head(-1))
                algo.update(self.data.head(-1))
            else:
                algo.update(self.data)
                
            if config.FrontEndDisplay:
                sendData = algo.update_frontend()
                algodata.append(sendData)
        
        # send data to front end
        if config.FrontEndDisplay:
            entry = self.data.tail(2).head(1)
            tickerdata = [{"ticker":self.name,"time":int(entry['time']), "open":float(entry['open']),"high":float(entry['high']),"low":float(entry['low']),"close":float(entry['close']),"volume":float(entry['volume'])}]
            payload = {"tickerdata":tickerdata,"algodata":algodata}
            self.socket.emit_data("update_send", payload)


    def intraminute_update(self):
        entry = self.data.tail(1)
        tickerdata = [{"ticker":self.name,"time":int(entry['time']), "open":float(entry['open']),"high":float(entry['high']),"low":float(entry['low']),"close":float(entry['close']),"volume":float(entry['volume'])}]
        payload = {"tickerdata":tickerdata}
        self.socket.emit_data("update_send", payload)


    def append(self, entry:list):
        if not config.LiveData:
            self.data = pd.concat([self.data,pd.DataFrame.from_records([entry])],ignore_index=True)
        else:
            df = pd.DataFrame(entry)
            df.columns=['date','time','open','high','low','close','volume']
            self.data = pd.concat([self.data,df],ignore_index=True)
        

    def replace(self, entry:list, pointNum = 1):
        self.data.drop(self.data.tail(pointNum).index,inplace=True)
        self.append(entry)