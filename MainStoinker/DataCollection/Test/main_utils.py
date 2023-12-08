import Start_config as config
from SocketIO_Client import FrontEndClient as sio
import simplejson
from livedatacollect4 import IBapi
from datetime import datetime
import time



def event_loop(event, index):
    while(not event.is_set()):
        event.wait()
        print("Event called for " + config.tickers[index].name)
        config.tickers[index].update_algos()
        event.clear()





def backtesting_data_blast():
    print("---Simulated Live Data Starting Now...---")
    ibape = IBapi()
    simulatedDatadict = ibape.simulatedDatadict
    socket = sio()

    #MOVE FRONT END STUFF SOMEWHERE ELSE
    if config.FrontEndDisplay:
        tickerfulldata = []
        socket.ConfigSend()
        for i in range(len(config.tickers)):
            Fulldata = getDataJson(index = i)
            tickerfulldata.append({'ticker': config.tickers[i].name, 'data':Fulldata})
        print("Sending Fulldata")
        try: 
            # self.socket.emit('update_send',{)
            payload = {"tickerdata":tickerfulldata}
            payload = simplejson.dumps(payload, ignore_nan=True)
            # print(payload)
            socket.sio.emit('data_send',payload)
        except Exception as e: 
            print(e)


    startpoint = config.tickers[0].data.shape[0]
    numpoints = simulatedDatadict[config.tickers[0].index].shape[0] - startpoint


    starttime = datetime.now()
    #for every point collected during backtesting
    for k in range(numpoints):
        #for all tickers in list
        for ticker in config.tickers.values():
            while(not config.updating):
                time.sleep(1)
            try:
                entry = simulatedDatadict[ticker.index].iloc[startpoint+k]
                ticker.append(entry)
            except Exception as e:
                print("hello this is justin telling you that the point you were looking for doesnt actually exist!")
                print("also paul says that we're in main_utils.backtesting_data_BLAST")
                print(e)
                time.sleep(10)
        
        # loop through tickers and update algos
        # we do this separate to get all data for minute first and then analyze
        for ticker in config.tickers.values():
            ticker.update_algos()

        time.sleep(config.TimeDelayPerPoint)
        print(entry[0])

    # self.printAlgoStats()
    endtime = datetime.now()
    duration = endtime-starttime
    print("Backtesting "+str(numpoints)+" Points is Done!")
    print("Duration: " + str(duration))
    print("___________________________________________________________")
    print("--------------Press 'CTRL' to Close Program----------------")
    print("___________________________________________________________")

def getDataJson(index):
    result = config.tickers[index].data.to_json(orient="records")
    # print(result)
    return(result)