import datetime
from dateutil.relativedelta import *

tickers = []
algos = []
LiveData = False
LiveTrading = False
#once trade excecution is figured out we should be able to get rid of one of these variables

Debug = True

#Frontend Variables
FrontEndDisplay = False
FrontEndPort = '192.168.0.212:3000'
updating = 1

#LiveData Variables
intraMinuteDisplay = True

#BackTesting Variables
StartDate = datetime.datetime.now() - relativedelta(month=0,weeks=0,day=1)
Duration = 2 #days
TimeDelayPerPoint = .1 #seconds between backtested points

timedelaytesty = 0

