const getColumn = async (columnNum) => {
  const res =  await fetch('./data.csv');
  const resp = await res.text();
  //console.log(resp);

    const cdata = resp.split('\n');
    if(cdata[cdata.length - 1] === ""){
      cdata.pop()
    }
    
    const gooddata = cdata.map((row) => {
      const r = row.split(',');
      var temptime = Date.parse(r[0])/1000;
      return{
        time: temptime,
        value: r[columnNum]*1
      } 
    });

    return gooddata;
};


const getData = async () => {
  const res =  await fetch('./data.csv');
  const resp = await res.text();
  //console.log(resp);

    const cdata = resp.split('\n');
    if(cdata[cdata.length - 1] === ""){
      cdata.pop()
    }

    const gooddata = cdata.map((row) => {
      const[time,open,high,low,close,volume,average,barcount] = row.split(',');
      
      var temptime = Date.parse(time)/1000;
      
      return{
        time: temptime,
        open: open*1,
        high: high*1,
        low: low*1,
        close: close*1,
      };
      
      
    });

    return gooddata;
  
  
};

//getData();

const displayChart = async () => {
  const chartProperties = {
    autoSize: true,
    timeScale:{
      timeVisibility: true,
      secondsVisible:true,
    },
  };

  const domElement = document.getElementById('tvchart');
  const chart = LightweightCharts.createChart(domElement, chartProperties);
  const candleSeries = chart.addCandlestickSeries();
  const klinedata = await getData();
  candleSeries.setData(klinedata);

  candleSeries.priceScale().applyOptions({
    autoScale: false, // disables auto scaling based on visible content
    scaleMargins: {
        top: 0.1,
        bottom: 0.2,
    },
  });

  chart.timeScale().applyOptions({
    secondsVisible: true,
    timeVisible: true,
  })

  const sma20_series = chart.addLineSeries({ color:'red', lineWidth:1});
  const sma20_data = await getColumn(8);
  //console.log(sma20_data);
  sma20_series.setData(sma20_data);

  const sma50_series = chart.addLineSeries({ color:'green', lineWidth:1});
  const sma50_data = await getColumn(9);
  sma50_series.setData(sma50_data);

  const LineTest = chart.addLineSeries({ color:'green', lineWidth:1});
  const LineData = await getColumn(10);
  LineTest.setData(LineData);
};

displayChart();