#-*- coding:utf-8 -*-    --------------Ashare 股票行情数据双核心版( https://github.com/mpquant/Ashare ) 
import json,requests,datetime;      import pandas as pd  #

def _normalize_ashare_code(code):
    """统一 A 股代码格式，确保腾讯/Sina 接口接受带交易所前缀的代码。"""
    code = str(code or '').strip()
    if not code:
        return code
    code = code.replace('.XSHG', '').replace('.XSHE', '').replace('.SH', '').replace('.SZ', '')
    if code.startswith(('600', '601', '603', '605', '688', '689')):
        return f'sh{code}'
    if code.startswith(('000', '001', '002', '003', '004', '300', '301')):
        return f'sz{code}'
    if code.startswith('bj'):
        return code
    return code


#---腾讯日线---  2025-12-21日正常使用
def get_price_day_tx(code, end_date='', count=10, frequency='1d'):     #日线获取  
    unit='week' if frequency in '1w' else 'month' if frequency in '1M' else 'day'     #判断日线，周线，月线
    code = _normalize_ashare_code(code)
    if end_date:
        end_date = end_date.strftime('%Y-%m-%d') if isinstance(end_date, datetime.date) else str(end_date).split(' ')[0]
    end_date='' if end_date==datetime.datetime.now().strftime('%Y-%m-%d') else end_date   #如果日期今天就变成空    
    URL=f'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},{unit},,{end_date},{count},qfq'
    try:
        response = requests.get(URL, timeout=20)
        response.raise_for_status()
        st = response.json() if hasattr(response, 'json') else json.loads(response.content)
    except Exception:
        return pd.DataFrame(columns=['time','open','close','high','low','volume'])
    data = st.get('data') if isinstance(st, dict) else None
    if not data or not isinstance(data, dict):
        return pd.DataFrame(columns=['time','open','close','high','low','volume'])
    stk = data.get(code)
    if not stk or not isinstance(stk, dict):
        return pd.DataFrame(columns=['time','open','close','high','low','volume'])
    ms='qfq'+unit
    buf = stk.get(ms) if ms in stk else stk.get(unit)
    if not buf:
        return pd.DataFrame(columns=['time','open','close','high','low','volume'])
    df=pd.DataFrame(buf,columns=['time','open','close','high','low','volume'],dtype='float')
    df.time=pd.to_datetime(df.time);    df.set_index(['time'], inplace=True);   df.index.name=''          #处理索引 
    return df

#腾讯分钟线
def get_price_min_tx(code, end_date=None, count=10, frequency='1d'):    #分钟线获取 
    ts=int(frequency[:-1]) if frequency[:-1].isdigit() else 1           #解析K线周期数
    if end_date: end_date=end_date.strftime('%Y-%m-%d') if isinstance(end_date,datetime.date) else end_date.split(' ')[0]        
    URL=f'http://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},m{ts},,{count}' 
    st= json.loads(requests.get(URL).content);       buf=st['data'][code]['m'+str(ts)] 
    df=pd.DataFrame(buf,columns=['time','open','close','high','low','volume','n1','n2'])   
    df=df[['time','open','close','high','low','volume']]    
    df[['open','close','high','low','volume']]=df[['open','close','high','low','volume']].astype('float')
    df.time=pd.to_datetime(df.time);   df.set_index(['time'], inplace=True);   df.index.name=''          #处理索引     
    df['close'][-1]=float(st['data'][code]['qt'][code][3])                #最新基金数据是3位的
    return df


#sina新浪全周期获取函数，分钟线 5m,15m,30m,60m  日线1d=240m   周线1w=1200m  1月=7200m
def get_price_sina(code, end_date='', count=10, frequency='60m'):    #新浪全周期获取函数    
    frequency=frequency.replace('1d','240m').replace('1w','1200m').replace('1M','7200m');   mcount=count
    ts=int(frequency[:-1]) if frequency[:-1].isdigit() else 1       #解析K线周期数
    if (end_date!='') & (frequency in ['240m','1200m','7200m']): 
        end_date = pd.to_datetime(end_date).to_pydatetime() if not isinstance(end_date, datetime.datetime) else end_date
        unit=4 if frequency=='1200m' else 29 if frequency=='7200m' else 1    #4,29多几个数据不影响速度
        count=count + (datetime.datetime.now() - end_date).days // unit            #结束时间到今天有多少天自然日(肯定 >交易日)        
        #print(code,end_date,count)    
    URL=f'http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale={ts}&ma=5&datalen={count}' 
    try:
        response = requests.get(URL, timeout=20)
        dstr = response.json() if hasattr(response, 'json') else json.loads(response.content)
    except Exception:
        return pd.DataFrame(columns=['day','open','high','low','close','volume'])
    if not dstr:
        return pd.DataFrame(columns=['day','open','high','low','close','volume'])
    df= pd.DataFrame(dstr,columns=['day','open','high','low','close','volume'])
    if df.empty:
        return df
    df['open'] = df['open'].astype(float); df['high'] = df['high'].astype(float);                          #转换数据类型
    df['low'] = df['low'].astype(float);   df['close'] = df['close'].astype(float);  df['volume'] = df['volume'].astype(float)
    df.day=pd.to_datetime(df.day);    df.set_index(['day'], inplace=True);     df.index.name=''            #处理索引
    if (end_date!='') & (frequency in ['240m','1200m','7200m']):
        return df[df.index<=end_date][-mcount:]
    return df


def get_price(code, end_date='',count=10, frequency='1d', fields=[]):        #对外暴露只有唯一函数，这样对用户才是最友好的  
    xcode = _normalize_ashare_code(code)

    if  frequency in ['1d','1w','1M']:   #1d日线  1w周线  1M月线
         try:    return get_price_sina( xcode, end_date=end_date,count=count,frequency=frequency)   #主力
         except Exception: return get_price_day_tx(xcode,end_date=end_date,count=count,frequency=frequency)   #备用
    
    if  frequency in ['1m','5m','15m','30m','60m']:  #分钟线 ,1m只有腾讯接口  5分钟5m   60分钟60m
         if frequency in '1m': return get_price_min_tx(xcode,end_date=end_date,count=count,frequency=frequency)
         try:    return get_price_sina(  xcode,end_date=end_date,count=count,frequency=frequency)   #主力
         except Exception: return get_price_min_tx(xcode,end_date=end_date,count=count,frequency=frequency)   #备用
        
if __name__ == '__main__':    
    df=get_price('sh000001',frequency='1d',count=10)      #支持'1d'日, '1w'周, '1M'月  
    print('上证指数日线行情\n',df)
    
    df=get_price('000001.XSHG',frequency='15m',count=10)  #支持'1m','5m','15m','30m','60m'
    print('上证指数分钟线\n',df)

# Ashare 股票行情数据( https://github.com/mpquant/Ashare ) 
