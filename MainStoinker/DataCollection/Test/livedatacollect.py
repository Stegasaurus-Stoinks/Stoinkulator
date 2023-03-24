from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import BarData
import datetime



class MyWrapper(EWrapper):

    def nextValidId(self, orderId:int):
        print("Setting nextValidOrderId: %d", orderId)
        self.nextValidOrderId = orderId
        self.start()

    def historicalData(self, reqId:int, bar: BarData):
        print("HistoricalData. ReqId:", reqId, "BarData.", bar)

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

app = EClient(MyWrapper())
app.connect("127.0.0.1", 7497, clientId=123)
lastbar = 0
app.run()