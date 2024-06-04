import datetime
from dateutil.relativedelta import *

tickers = {}
algos = []
LiveData = False
LiveTrading = False
#once trade excecution is figured out we should be able to get rid of one of these variables

Debug = False

#Frontend Variables
FrontEndDisplay = False
FrontEndPort = '24.56.52.6:3000'
updating = 1
tickerIndex = 0
#LiveData Variables
intraMinuteDisplay = True

#BackTesting Variables
Duration = 2 #days
StartDate = datetime.datetime.now() - relativedelta(month=0,weeks=0,day=Duration)
TimeDelayPerPoint = 0 #seconds between backtested points

timedelaytesty = 0

