from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import BarData
from csv import writer
import datetime




class MyWrapper(EWrapper):

    def nextValidId(self, orderId:int):
        print("Setting nextValidOrderId: %d", orderId)
        self.nextValidOrderId = orderId
        self.start()

    def historicalData(self, reqId:int, bar: BarData):
        print("HistoricalData. ReqId:", reqId, "BarData.", bar)
        candleData = [bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]
        with open('./MainStoinker/DataCollection/Test/datalive.csv', 'a') as f_object:
        
            # Pass this file object to csv.writer()
            # and get a writer object
            writer_object = writer(f_object)
        
            # Pass the list as an argument into
            # the writerow()
            writer_object.writerow(candleData)
        
            # Close the file object
            f_object.close()

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        print("All Historical Data Collected: Live Data Starting Now...")
        #app.disconnect()
        #print("Finished")

    def historicalDataUpdate(self, reqId: int, bar: BarData):
        global lastbar
        if lastbar != 0:
            if(bar.average != lastbar.average):
                print("HistoricalDataUpdate. ReqId:", reqId, "BarData.", bar)
                
              
                # Open our existing CSV file in append mode
                # Create a file object for this file
                candleData = [bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]
                with open('./MainStoinker/DataCollection/Test/datalive.csv', 'a') as f_object:
                
                    # Pass this file object to csv.writer()
                    # and get a writer object
                    writer_object = writer(f_object)
                
                    # Pass the list as an argument into
                    # the writerow()
                    writer_object.writerow(candleData)
                
                    # Close the file object
                    f_object.close()

                lastbar = bar

        else:
            lastbar = bar


        
        

    def error(self, reqId, errorCode, errorString):
        print("Error. Id: " , reqId, " Code: " , errorCode , " Msg: " , errorString)

    def start(self):
        queryTime = (datetime.datetime.today() - datetime.timedelta(days=0)).strftime("%Y%m%d %H:%M:%S")

        contract = Contract()
        contract.secType = "STK"
        contract.symbol = "AAPL"
        contract.currency = "USD"
        contract.exchange = "SMART"

        app.reqHistoricalData(1, contract, "", "2 D", "1 min", "TRADES", 1, 1, True, [])

        f = open("./MainStoinker/DataCollection/Test/datalive.csv", "w")
        f.truncate()
        f.close()

