import datetime
import queue
import threading

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import BarData
from ibapi.common import *

import pandas as pd
from datetime import datetime
from datetime import timedelta

def createStockContact(ticker: str):
    contract = Contract()
    contract.secType = "STK"
    contract.symbol = ticker
    contract.currency = "USD"
    contract.exchange = "SMART"

    return contract

class IBAPIWrapper(EWrapper):
    """
    A derived subclass of the IB API EWrapper interface
    that allows more straightforward response processing
    from the IB Gateway or an instance of TWS.
    """

    def init_error(self):
        """
        Place all of the error messages from IB into a
        Python queue, which can be accessed elsewhere.
        """
        error_queue = queue.Queue()
        self._errors = error_queue

    def is_error(self):
        """
        Check the error queue for the presence
        of errors.

        Returns
        -------
        `boolean`
            Whether the error queue is not empty
        """
        return not self._errors.empty()

    def get_error(self, timeout=5):
        """
        Attempts to retrieve an error from the error queue,
        otherwise returns None.

        Parameters
        ----------
        timeout : `float`
            Time-out after this many seconds.

        Returns
        -------
        `str` or None
            A potential error message from the error queue.
        """
        if self.is_error():
            try:
                return self._errors.get(timeout=timeout)
            except queue.Empty:
                return None
        return None

    def error(self, id, errorCode, errorString):
        """
        Format the error message with appropriate codes and
        place the error string onto the error queue.
        """
        error_message = (
            "IB Error ID (%d), Error Code (%d) with "
            "response '%s'" % (id, errorCode, errorString)
        )
        self._errors.put(error_message)

    def init_time(self):
        """
        Instantiates a new queue to store the server
        time, assigning it to a 'private' instance
        variable and also returning it.

        Returns
        -------
        `Queue`
            The time queue instance.
        """
        time_queue = queue.Queue()
        self._time_queue = time_queue
        return time_queue

    def currentTime(self, server_time):
        """
        Takes the time received by the server and
        appends it to the class instance time queue.

        Parameters
        ----------
        server_time : `str`
            The server time message.
        """
        self._time_queue.put(server_time)


class IBAPIClient(EClient):
    """
    Used to send messages to the IB servers via the API. In this
    simple derived subclass of EClient we provide a method called
    obtain_server_time to carry out a 'sanity check' for connection
    testing.

    Parameters
    ----------
    wrapper : `EWrapper` derived subclass
        Used to handle the responses sent from IB servers
    """

    MAX_WAIT_TIME_SECONDS = 10

    def __init__(self, wrapper):
        EClient.__init__(self, wrapper)

    def obtain_server_time(self):
        """
        Requests the current server time from IB then
        returns it if available.

        Returns
        -------
        `int`
            The server unix timestamp.
        """
        # Instantiate a queue to store the server time
        time_queue = self.wrapper.init_time()

        # Ask IB for the server time using the EClient method
        self.reqCurrentTime()

        # Try to obtain the latest server time if it exists
        # in the queue, otherwise issue a warning
        try:
            server_time = time_queue.get(
                timeout=IBAPIClient.MAX_WAIT_TIME_SECONDS
            )
        except queue.Empty:
            print(
                "Time queue was empty or exceeded maximum timeout of "
                "%d seconds" % IBAPIClient.MAX_WAIT_TIME_SECONDS
            )
            server_time = None

        # Output all additional errors, if they exist
        while self.wrapper.is_error():
            print(self.get_error())

        return server_time


class IBAPIApp(IBAPIWrapper, IBAPIClient):
    """
    The IB API application class creates the instances
    of IBAPIWrapper and IBAPIClient, through a multiple
    inheritance mechanism.

    When the class is initialised it connects to the IB
    server. At this stage multiple threads of execution
    are generated for the client and wrapper.

    Parameters
    ----------
    ipaddress : `str`
        The IP address of the TWS client/IB Gateway
    portid : `int`
        The port to connect to TWS/IB Gateway with
    clientid : `int`
        An (arbitrary) client ID, that must be a positive integer
    """

    def __init__(self, ipaddress, portid, clientid):
        IBAPIWrapper.__init__(self)
        IBAPIClient.__init__(self, wrapper=self)

        # Connects to the IB server with the
        # appropriate connection parameters
        self.connect(ipaddress, portid, clientid)

        # Initialise the threads for various components
        thread = threading.Thread(target=self.run)
        thread.start()
        setattr(self, "_thread", thread)

        # Listen for the IB responses
        self.init_error()

        self.all_positions = pd.DataFrame([], columns = ['Account','Symbol', 'Quantity', 'Average Cost', 'Sec Type'])
        self.all_accounts = pd.DataFrame([], columns = ['reqId','Account', 'Tag', 'Value' , 'Currency'])
        self.all_openorders = pd.DataFrame([], columns = ['Symbol', 'Order Type', 'Quantity', 'Action', 'Order State', 'Sec Type'])
        

    #Generate new list of positions, returns Pandas DataFrame
    def readPositions(self,tickerSymbol:str = None):
        self.positions_event_obj = threading.Event()
        self.temp = self.reqPositions() # associated callback: position
        # self.reqPositionsMulti()
        # if config.Debug:
        #     print("Waiting for IB's API response for accounts positions requests...")
        # time.sleep(3)
        timeout = 2
        flag = self.positions_event_obj.wait(timeout)
        if flag:
            current_positions = self.all_positions # associated callback: position
            # dont know why i cant shift the index of the array, adding line below breaks stuff :(
            # current_positions.set_index('Account',inplace=True,drop=True) #set all_positions DataFrame index to "Account"
            if tickerSymbol:
                return current_positions.loc[current_positions['Symbol'] == tickerSymbol]
            return current_positions
        else:
            print("error with callback for positions")
    
    def position(self, account, contract, pos, avgCost):
        index = str(account)+str(contract.symbol)
        # if config.Debug:
        #     print("In CallBack for Postions: Position Data Received")
        #     print(account, contract.symbol, pos, avgCost, contract.secType)
        self.all_positions.loc[index]= {'Account':account, 'Symbol':contract.symbol, 'Quantity':pos, 'Average Cost':avgCost, 'Sec Type':contract.secType}
            
    def positionEnd(self, event_obj = None):
        # super().positionEnd()
        # if config.Debug:
        #     print("PositionEnd CallBack")
        try:
            self.positions_event_obj.set()
        except Exception as e:
            print(e)
            print("failed to set event object for readPositions")

    def historicalData(self, reqId: int, bar: BarData):
        # print("HistoricalData. ReqId:", reqId, "BarData.", bar)
        candleData = [datetime.fromtimestamp(int(bar.date)),int(bar.date), bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]

        self.simulatedDatadict[reqId] = self.simulatedDatadict[reqId]._append([candleData], ignore_index=True)


    def historicalDataEnd(self, reqId: int, start: str, end: str):
        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        
        self.simulatedDatadict[reqId].columns=['date','time','open','high','low','close','volume','average']
        print("Historical Data Collected for AAPL")
        self.datacollectednum += 1
        # print(self.simulatedDatadict[reqId])
        # Create datadict data frame here
        # ____________________________________________________________________________________
        firstDate = self.simulatedDatadict[reqId].at[0,'date']
        startDate = firstDate + timedelta(days=1)
        print("Warmup Start Date: " + str(firstDate))
        print("Warmup End Date: " + str(startDate))
        self.datadict[reqId] = self.simulatedDatadict[reqId].loc[(self.simulatedDatadict[reqId]['date'] < startDate)]
        self.datadict[reqId].columns=['date','time', 'open','high','low','close','volume','average']
        # print(self.datadict[reqId])

        if self.datacollectednum >= 1: #all historical data collected
            print("------All Historical Data Collected------")
            print("---Simulated Live Data Starting Now...---")

            print("historical data end read positions:")
            print(self.readPositions())

            # threadtest = threading.Thread(target=self.backtestingDataUpdate(),daemon=True)
            # threadtest.start()
            # self.backtestingDataUpdate()

    def startData(self):
        i = 0
        self.datadict = {}
        self.simulatedDatadict = {}
        self.livetickerdata = []
        self.liveintraminutedata = []
        self.lastbardict = {}
        

        contract = createStockContact("AAPL")

        self.datacollectednum = 0 #variable to track completed historical data pulls
        self.reqHistoricalData(i, contract, "", str(2) + " D", "1 min", "TRADES", 1, 2, False, [])
        self.simulatedDatadict[i] = pd.DataFrame()
        self.datacollectednum = 0

            
        self.datadict[i] = pd.DataFrame()
        self.lastbardict[i] = 0
        i += 1

        print("startData read positions")
        print(self.readPositions())

    


if __name__ == '__main__':
    # Application parameters
    host = '127.0.0.1'  # Localhost, but change if TWS is running elsewhere
    port = 7497  # Change to the appropriate IB TWS account port number
    client_id = 123

    print("Launching IB API application...")

    # Instantiate the IB API application
    app = IBAPIApp(host, port, client_id)

    print("Successfully launched IB API application...")

    # Obtain the server time via the IB API app
    server_time = app.obtain_server_time()
    server_time_readable = datetime.utcfromtimestamp(
        server_time
    ).strftime('%Y-%m-%d %H:%M:%S')

    print("Current IB server time: %s" % server_time_readable)

    print(app.readPositions())
    app.startData()

    # Disconnect from the IB server
    # app.disconnect()

    print("Disconnected from the IB API application. Finished.")



