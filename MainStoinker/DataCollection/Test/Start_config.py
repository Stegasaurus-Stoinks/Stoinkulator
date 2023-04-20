import datetime
from dateutil.relativedelta import *

LiveData = False
LiveTrading = False
#once trade excecution is figured out we should be able to get rid of one of these variables

StartDate = datetime.datetime.now() - relativedelta(month=0,weeks=0,day=2)
Duration = '2 D'

intraMinutePrint = True

