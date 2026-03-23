# FrontEndDataType = ['marker-up','marker-down','line','line','line','line','segment','baseline']

# for entry in FrontEndDataType:
#     if "marker-" in entry:
#         print(entry)

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from MainStoinker.DataCollection.apiApi import ibapi as app
import time
import threading
import keyboard

import tkinter as tk

from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import *

from MainStoinker.Util.IBKRHelper import *

import pandas

QUANTITY = 10
SYMBOL = 'AAPL'
STOPLOSS = 1
STOPLOSS_ENABLE = 0

trades = [1,2,3,4,5]
tradesfrontend = []

# app is the module-level ibapi instance imported above

root = tk.Tk()

qty_entry = ''
ticker_entry = ''
stoploss_entry = ''
stoploss_enable_var = tk.IntVar()

def main():
    global qty_entry,ticker_entry,stoploss_entry,stoploss_enable
    
    app.connect('127.0.0.1', 7497, 123)

    while(not app.isConnected()):
        print("TWS Connection ", str(app.isConnected()))
        time.sleep(2)
    time.sleep(1)
    print("TWS Connected")

    api_thread = threading.Thread(target=app.run,daemon=True)
    api_thread.start()

    if not app.getNextOrderID():
            print("Something wrong with connection (no response from TWS)")
            print("Shutting Down...")
            time.sleep(1)
            app.disconnect
            time.sleep(1)
            exit()

    else:
        print("Conenction Successful")


# Main Title
    tk.Label(root,
             text="Live Trading Testing GUI",
             justify="center",
             ).pack(padx=100,pady=5)
    
# Account Info Frame  
    account_frame = tk.Frame(root, width=400, height=400, bg="skyblue")
    account_frame.pack(padx=5, pady=5, side=tk.LEFT, fill=tk.Y)
    tk.Label(account_frame,
             text="ACCOUNT INFO"
             ).pack(padx=50,pady=5)
    
    #trades sub frame
    trade_list_subframe = tk.Frame(account_frame,width=390, height=300)
    trade_list_subframe.pack(padx=5,pady=5)
    tk.Label(trade_list_subframe,text="Trade Log").pack(padx=5,pady=5)

    #individual trade sub frame
    for trade in trades:
        tradeindex = trade
        trade_subframe = tk.Frame(trade_list_subframe,width=350,height=40, bg="skyblue")
        trade_subframe.pack(padx=5,pady=5)
        tk.Label(trade_subframe,text="Trade #" + str(tradeindex)).pack(padx=40,pady=5)


# Trading Frame
    trading_frame = tk.Frame(root,width=600,height=600,bg="skyblue")
    trading_frame.pack(padx=5,pady=5,side=tk.RIGHT)
    tk.Label(trading_frame,
             text="TRADING"
             ).pack(padx=50,pady=5)
    
    quantity_subframe = tk.Frame(trading_frame,width=590,height=20)
    quantity_subframe.pack(padx=5,pady=5)
    
    qty_label = tk.Label(quantity_subframe, textvariable=tk.StringVar(value="Quantity:"),)
    qty_label.pack(side=tk.LEFT,pady=10)

    qty_entry = tk.Entry(quantity_subframe)
    qty_entry.insert(tk.END,QUANTITY)
    qty_entry.pack(side=tk.LEFT, pady=10)

    ticker_subframe = tk.Frame(trading_frame,width=590,height=20)
    ticker_subframe.pack(padx=5,pady=5)
    
    ticker_label = tk.Label(ticker_subframe, textvariable=tk.StringVar(value="Ticker:"),)
    ticker_label.pack(side=tk.LEFT,pady=10)

    ticker_entry = tk.Entry(ticker_subframe)
    ticker_entry.insert(tk.END,SYMBOL)
    ticker_entry.pack(side=tk.LEFT, pady=10)

    stoploss_subframe = tk.Frame(trading_frame,width=590,height=20)
    stoploss_subframe.pack(padx=5,pady=5)

    stoploss_check_button = tk.Checkbutton(stoploss_subframe, text = "Stoploss (%) :",variable=stoploss_enable_var,onvalue=1,offvalue=0,height=2,width=10)
    stoploss_check_button.pack(side=tk.LEFT,pady=10)

    stoploss_entry = tk.Entry(stoploss_subframe)
    stoploss_entry.insert(tk.END,STOPLOSS)
    stoploss_entry.pack(side=tk.LEFT, pady=10)


    # Creating a button with specified options
    buybutton = tk.Button(trading_frame, 
                    text="Buy", 
                    command=buy_button_clicked,
                    activebackground="blue", 
                    activeforeground="white",
                    anchor="center",
                    bd=3,
                    bg="lightgray",
                    cursor="hand2",
                    disabledforeground="gray",
                    fg="black",
                    font=("Arial", 12),
                    height=2,
                    highlightbackground="black",
                    highlightcolor="green",
                    highlightthickness=2,
                    justify="center",
                    overrelief="raised",
                    padx=10,
                    pady=5,
                    width=15,
                    wraplength=100)

    sellbutton = tk.Button(trading_frame,
                    text="Sell", 
                    command=sell_button_clicked,
                    activebackground="blue", 
                    activeforeground="white",
                    anchor="center",
                    bd=3,
                    bg="lightgray",
                    cursor="hand2",
                    disabledforeground="gray",
                    fg="black",
                    font=("Arial", 12),
                    height=2,
                    highlightbackground="black",
                    highlightcolor="green",
                    highlightthickness=2,
                    justify="center",
                    overrelief="raised",
                    padx=10,
                    pady=5,
                    width=15,
                    wraplength=100)

    buybutton.pack(padx=20, pady=20)
    sellbutton.pack(padx=20, pady=20)

    root.mainloop()

def update_entry_values():
    global SYMBOL
    global QUANTITY
    global STOPLOSS, STOPLOSS_ENABLE

    SYMBOL = ticker_entry.get()
    QUANTITY = qty_entry.get()
    STOPLOSS = stoploss_entry.get()
    STOPLOSS_ENABLE = stoploss_enable_var.get()

def sell_order_object(quantity, limitPrice = None):
    order = Order()
    order.action = "Sell"
    order.totalQuantity = quantity
    if limitPrice == None:
        order.orderType =  "MKT"
    else:
        order.orderType = "LMT"
        order.lmtPrice = limitPrice
    order.eTradeOnly = False
    order.firmQuoteOnly = False

    return order

def buy_order_object(quantity, limitPrice = None):
    order = Order()
    order.action = "Buy"
    order.totalQuantity = quantity
    if limitPrice == None:
        order.orderType =  "MKT"
    else:       
        order.orderType = "LMT"
        order.lmtPrice = limitPrice
    order.eTradeOnly = False
    order.firmQuoteOnly = False
    # order.adjustedStopLimitPrice = stopPrice

    return order

def buy_button_clicked():
    print("Buy Button clicked!")

    update_entry_values()

    if(STOPLOSS_ENABLE):
        print("Creating Buy Trade with " + STOPLOSS + "% Stoploss")


    app.getNextOrderID()
    parentId = app.nextValidOrderId

    symbol = SYMBOL
    volume = QUANTITY
    price = 212.00

    contract = create_stock_contract(symbol)

    parentOrder = buy_order_object(volume)

    app.placeOrder(parentId,contract,parentOrder)

def sell_button_clicked():
    print("Sell Button clicked!")

    update_entry_values()

    if(STOPLOSS_ENABLE):
        print("Creating Sell Trade with " + STOPLOSS + "% Stoploss")

    app.getNextOrderID()
    parentId = app.nextValidOrderId

    symbol = SYMBOL
    volume = QUANTITY
    price = 212.00

    contract = create_stock_contract(symbol)

    parentOrder = sell_order_object(volume)

    app.placeOrder(parentId,contract,parentOrder)


main()

