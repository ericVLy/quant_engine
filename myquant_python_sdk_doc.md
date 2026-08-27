# 掘金量化 Python SDK 文档

---

# 快速开始

常见的策略结构主要包括 3 类，如下图所示。用户可以根据策略需求选择相应的策略结构，具体可以参考[经典策略](/docs2/operatingInstruction/study/示例策略.html)。

## 定时任务示例

以下代码的内容是：在每个交易日的 14:50:00 市价买入 200 股浦发银行股票：

```python
# coding=utf-8
from __future__ import print_function, absolute_import
from gm.api import *

def init(context):
    # 每天14:50 定时执行algo任务
    # algo执行定时任务函数，只能传context参数
    # date_rule执行频率，目前暂时支持1d、1w、1m，其中1w、1m仅用于回测，实时模式1d以上的频率，需要在algo判断日期
    # time_rule执行时间， 注意多个定时任务设置同一个时间点，前面的定时任务会被后面的覆盖
    schedule(schedule_func=algo, date_rule='1d', time_rule='14:50:00')

def algo(context):
    # 以市价购买200股浦发银行股票，price在市价类型不生效
    order_volume(symbol='SHSE.600000', volume=200, side=OrderSide_Buy,
                 order_type=OrderType_Market, position_effect=PositionEffect_Open, price=0)

# 查看最终的回测结果
def on_backtest_finished(context, indicator):
    print(indicator)

if __name__ == '__main__':
    '''
    strategy_id策略ID, 由系统生成
    filename文件名, 请与本文件名保持一致
    mode运行模式, 实时模式:MODE_LIVE回测模式:MODE_BACKTEST
    token绑定计算机的ID, 可在系统设置-密钥管理中生成
    backtest_start_time回测开始时间
    backtest_end_time回测结束时间
    backtest_adjust股票复权方式, 不复权:ADJUST_NONE前复权:ADJUST_PREV后复权:ADJUST_POST
    backtest_initial_cash回测初始资金
    backtest_commission_ratio回测佣金比例
    backtest_slippage_ratio回测滑点比例
    '''
    run(strategy_id='strategy_id', filename='main.py', mode=MODE_BACKTEST,
        token='token_id', backtest_start_time='2020-11-01 08:00:00',
        backtest_end_time='2020-11-10 16:00:00', backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=10000000, backtest_commission_ratio=0.0001,
        backtest_slippage_ratio=0.0001)
```

整个策略需要三步：
1. 设置初始化函数：[init](/docs2/sdk/python/API介绍/基本函数.html#init-初始化策略)，使用 [schedule](/docs2/sdk/python/API介绍/基本函数.html#schedule-定时任务配置) 函数进行定时任务配置
2. 配置任务，到点会执行该任务
3. 执行策略

## 数据事件驱动示例

在用 `subscribe()` 接口订阅标的后，后台会返回 tick 数据或 bar 数据。每产生一个或一组数据，就会自动触发 `on_tick()` 或 `on_bar()` 里面的内容执行。比如以下范例代码片段，订阅浦发银行频率为 1 天和 60s 的 bar 数据，每产生一次 bar，就会自动触发 `on_bar()` 调用，打印获取的 bar 信息：

```python
# coding=utf-8
from __future__ import print_function, absolute_import
from gm.api import *

def init(context):
    # 订阅浦发银行, bar频率为一天和一分钟
    # 订阅订阅多个频率的数据，可多次调用subscribe
    subscribe(symbols='SHSE.600000', frequency='1d')
    subscribe(symbols='SHSE.600000', frequency='60s')

def on_bar(context, bars):
    # 打印bar数据
    print(bars)

if __name__ == '__main__':
    '''
    strategy_id策略ID, 由系统生成
    filename文件名, 请与本文件名保持一致
    mode运行模式, 实时模式:MODE_LIVE回测模式:MODE_BACKTEST
    token绑定计算机的ID, 可在系统设置-密钥管理中生成
    backtest_start_time回测开始时间
    backtest_end_time回测结束时间
    backtest_adjust股票复权方式, 不复权:ADJUST_NONE前复权:ADJUST_PREV后复权:ADJUST_POST
    backtest_initial_cash回测初始资金
    backtest_commission_ratio回测佣金比例
    backtest_slippage_ratio回测滑点比例
    '''
    run(strategy_id='strategy_id', filename='main.py', mode=MODE_BACKTEST,
        token='token_id', backtest_start_time='2020-11-01 08:00:00',
        backtest_end_time='2020-11-10 16:00:00', backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=10000000, backtest_commission_ratio=0.0001,
        backtest_slippage_ratio=0.0001)
```

整个策略需要三步：
1. 设置初始化函数：`init`，使用 `subscribe` 函数进行数据订阅
2. 实现一个函数：`on_bar`，来根据数据推送进行逻辑处理
3. 执行策略

## 时间序列数据事件驱动示例

策略订阅代码时指定数据窗口大小与周期，平台创建数据滑动窗口，加载初始数据，并在新的 bar 到来时自动刷新数据。`on_bar` 事件触发时，策略可以通过 `context.data` 取到订阅代码的准备好的时间序列数据。

以下的范例代码片段是一个非常简单的例子，订阅浦发银行的日线和分钟 bar，bar 数据的更新会自动触发 `on_bar` 的调用，每次调用 `context.data` 来获取最新的 50 条分钟 bar 信息：

```python
# coding=utf-8
from __future__ import print_function, absolute_import
from gm.api import *

def init(context):
    # 订阅浦发银行, bar频率为一天和一分钟
    # 指定数据窗口大小为50
    # 订阅订阅多个频率的数据，可多次调用subscribe
    subscribe(symbols='SHSE.600000', frequency='1d', count=50, format='df',
              fields='symbol, close, eob')
    subscribe(symbols='SHSE.600000', frequency='60s', count=50, format='df',
              fields='symbol, close, eob')

def on_bar(context, bars):
    # context.data提取缓存的数据滑窗, 可用于计算指标
    # 注意：context.data里的count要小于或者等于subscribe里的count,fields需要在subscribe的fields范围内
    data = context.data(symbol=bars[0]['symbol'], frequency='60s', count=50,
                        fields='close,eob')
    # 计算均线
    data['ma5'] = data['close'].rolling(window=5).mean()
    # 打印最后5条bar数据（最后一条是最新的bar）
    print(data.tail())

if __name__ == '__main__':
    '''
    strategy_id策略ID, 由系统生成
    filename文件名, 请与本文件名保持一致
    mode运行模式, 实时模式:MODE_LIVE回测模式:MODE_BACKTEST
    token绑定计算机的ID, 可在系统设置-密钥管理中生成
    backtest_start_time回测开始时间
    backtest_end_time回测结束时间
    backtest_adjust股票复权方式, 不复权:ADJUST_NONE前复权:ADJUST_PREV后复权:ADJUST_POST
    backtest_initial_cash回测初始资金
    backtest_commission_ratio回测佣金比例
    backtest_slippage_ratio回测滑点比例
    '''
    run(strategy_id='strategy_id', filename='main.py', mode=MODE_BACKTEST,
        token='token_id', backtest_start_time='2020-11-01 08:00:00',
        backtest_end_time='2020-11-10 16:00:00', backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=10000000, backtest_commission_ratio=0.0001,
        backtest_slippage_ratio=0.0001)
```

整个策略需要三步：
1. 设置初始化函数：[init](/docs2/sdk/python/API介绍/基本函数.html#init-初始化策略)，使用 [subscribe](/docs2/sdk/python/API介绍/数据订阅.html#subscribe-行情订阅) 函数进行数据订阅
2. 实现一个函数：[on_bar](/docs2/sdk/python/API介绍/数据事件.html#on-bar-bar-数据推送事件)，来根据数据推送进行逻辑处理，通过 `context.data` 获取数据滑窗
3. 执行策略

## 选择回测模式/实时模式运行示例

掘金 3 策略只有两种模式，回测模式(backtest)与实时模式(live)。在加载策略时指定 `mode` 参数。

```python
# coding=utf-8
from __future__ import print_function, absolute_import
from gm.api import *

def init(context):
    # 订阅浦发银行的tick
    subscribe(symbols='SHSE.600000', frequency='60s')

def on_bar(context, bars):
    # 打印当前获取的bar信息
    print(bars)

if __name__ == '__main__':
    # 在终端仿真交易和实盘交易的启动策略按钮默认是实时模式，运行回测默认是回测模式，在外部IDE里运行策略需要修改成对应的运行模式
    # mode=MODE_LIVE 实时模式, 回测模式的相关参数不生效
    # mode=MODE_BACKTEST 回测模式
    '''
    strategy_id策略ID, 由系统生成
    filename文件名, 请与本文件名保持一致
    mode运行模式, 实时模式:MODE_LIVE回测模式:MODE_BACKTEST
    token绑定计算机的ID, 可在系统设置-密钥管理中生成
    backtest_start_time回测开始时间
    backtest_end_time回测结束时间
    backtest_adjust股票复权方式, 不复权:ADJUST_NONE前复权:ADJUST_PREV后复权:ADJUST_POST
    backtest_initial_cash回测初始资金
    backtest_commission_ratio回测佣金比例
    backtest_slippage_ratio回测滑点比例
    '''
    run(strategy_id='strategy_id', filename='main.py', mode=MODE_LIVE,
        token='token_id', backtest_start_time='2020-11-01 08:00:00',
        backtest_end_time='2020-11-10 16:00:00', backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=10000000, backtest_commission_ratio=0.0001,
        backtest_slippage_ratio=0.0001)
```

整个策略需要三步：
1. 设置初始化函数：[init](/docs2/sdk/python/API介绍/基本函数.html#init-初始化策略)，使用 [subscribe](/docs2/sdk/python/API介绍/数据订阅.html#subscribe-行情订阅) 函数进行数据订阅代码
2. 实现一个函数：[on_bar](/docs2/sdk/python/API介绍/数据事件.html#on-bar-bar-数据推送事件)，来根据数据推送进行逻辑处理
3. 选择对应模式，执行策略

## 提取数据研究示例

如果只想提取数据，无需实时数据驱动策略，无需交易下单可以直接通过数据查询函数来进行查询。

```python
# coding=utf-8
from __future__ import print_function, absolute_import
from gm.api import *

# 可以直接提取数据，掘金终端需要打开，接口取数是通过网络请求的方式，效率一般，行情数据可通过subscribe订阅方式
# 设置token，查看已有token ID,在用户-密钥管理里获取
set_token('your token_id')

# 查询历史行情, 采用定点复权的方式，adjust指定前复权，adjust_end_time指定复权时间点
data = history(symbol='SHSE.600000', frequency='1d',
               start_time='2020-01-01 09:00:00', end_time='2020-12-31 16:00:00',
               fields='open,high,low,close', adjust=ADJUST_PREV,
               adjust_end_time='2020-12-31', df=True)
print(data)
```

整个过程只需要两步：
1. `set_token` 设置用户 token，如果 token 不正确，函数调用会抛出异常
2. 调用数据查询函数，直接进行数据查询

## 回测模式下高速处理数据示例

本示例提供一种在 `init` 中预先取全集数据，规整后索引调用的高效数据处理方式，能够避免反复调用服务器接口导致的低效率问题，可根据该示例思路，应用到其他数据接口以提高效率。

```python
# coding=utf-8
from __future__ import print_function, absolute_import
from gm.api import *

def init(context):
    # 在init中一次性拿到所有需要的instruments信息
    instruments = get_history_symbol(symbol='SZSE.000001',
                                     start_date=context.backtest_start_time, end_date=context.backtest_end_time)
    # 将信息按symbol,date作为key存入字典
    context.ins_dict = {(i.symbol, i.trade_date.date()): i for i in instruments}
    subscribe(symbols='SZSE.000001', frequency='1d')

def on_bar(context, bars):
    print(context.ins_dict[(bars[0].symbol, bars[0].eob.date())])

if __name__ == '__main__':
    '''
    strategy_id策略ID, 由系统生成
    filename文件名, 请与本文件名保持一致
    mode运行模式, 实时模式:MODE_LIVE回测模式:MODE_BACKTEST
    token绑定计算机的ID, 可在系统设置-密钥管理中生成
    backtest_start_time回测开始时间
    backtest_end_time回测结束时间
    backtest_adjust股票复权方式, 不复权:ADJUST_NONE前复权:ADJUST_PREV后复权:ADJUST_POST
    backtest_initial_cash回测初始资金
    backtest_commission_ratio回测佣金比例
    backtest_slippage_ratio回测滑点比例
    '''
    run(strategy_id='strategy_id', filename='main.py', mode=MODE_BACKTEST,
        token='token_id', backtest_start_time='2020-11-01 08:00:00',
        backtest_end_time='2020-11-10 16:00:00', backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=10000000, backtest_commission_ratio=0.0001,
        backtest_slippage_ratio=0.0001)
```

整个策略需要三步：
1. 设置初始化函数：[init](/docs2/sdk/python/API介绍/基本函数.html#init-初始化策略)，一次性拿到所有需要的 `instruments` 信息，将信息按 `symbol,date` 作为 key 存入字典，使用 [subscribe](/docs2/sdk/python/API介绍/数据订阅.html#subscribe-行情订阅) 函数进行数据订阅代码
2. 实现一个函数：[on_bar](/docs2/sdk/python/API介绍/数据事件.html#on-bar-bar-数据推送事件)，来根据数据推送进行逻辑处理
3. 执行策略

## 实时模式下动态参数示例

本示例提供一种通过策略设置动态参数，可在终端界面显示和修改，在不停止策略的情况下手动修改参数传入策略方法。

```python
# coding=utf-8
from __future__ import print_function, absolute_import, unicode_literals
from gm.api import *
import numpy as np
import pandas as pd

'''
动态参数，是指在不终止策略的情况下，掘金终端UI界面和策略变量做交互，
通过add_parameter在策略代码里设置动态参数，终端UI界面会显示对应参数
'''
def init(context):
    # log日志函数，只支持实时模式，在仿真交易和实盘交易界面查看，重启终端log日志会被清除，需要记录到本地可以使用logging库
    log(level='info', msg='平安银行信号触发', source='strategy')
    # 设置k值阀值作为动态参数
    context.k_value = 23
    # add_parameter设置动态参数函数，只支持实时模式，在仿真交易和实盘交易界面查看，重启终端动态参数会被清除，重新运行策略会重新设置
    add_parameter(key='k_value', value=context.k_value, min=0, max=100, name='k值阀值',
                  intro='设置k值阀值', group='1', readonly=False)
    # 设置d值阀值作为动态参数
    context.d_value = 20
    add_parameter(key='d_value', value=context.d_value, min=0, max=100, name='d值阀值',
                  intro='设置d值阀值', group='2', readonly=False)
    print('当前的动态参数有', context.parameters)
    # 订阅行情
    subscribe(symbols='SZSE.002400', frequency='60s', count=120)

def on_bar(context, bars):
    data = context.data(symbol=bars[0]['symbol'], frequency='60s', count=100)
    kdj = KDJ(data, 9, 3, 3)
    k_value = kdj['kdj_k'].values
    d_value = kdj['kdj_d'].values
    if k_value[-1] > context.k_value and d_value[-1] < context.d_value:
        order_percent(symbol=bars[0]['symbol'], percent=0.01, side=OrderSide_Buy,
                      order_type=OrderType_Market, position_effect=PositionEffect_Open)
        print('{}下单买入， k值为{}'.format(bars[0]['symbol'], context.k_value))

# 计算KDJ
def KDJ(data, N, M1, M2):
    lowList = data['low'].rolling(N).min()
    lowList.fillna(value=data['low'].expanding().min(), inplace=True)
    highList = data['high'].rolling(N).max()
    highList.fillna(value=data['high'].expanding().max(), inplace=True)
    rsv = (data['close'] - lowList) / (highList - lowList) * 100
    data['kdj_k'] = rsv.ewm(alpha=1/M1).mean()
    data['kdj_d'] = data['kdj_k'].ewm(alpha=1/M2).mean()
    data['kdj_j'] = 3.0 * data['kdj_k'] - 2.0 * data['kdj_d']
    return data

# 动态参数变更事件
def on_parameter(context, parameter):
    # print(parameter)
    if parameter['name'] == 'k值阀值':
        # 通过全局变量把动态参数值传入别的事件里
        context.k_value = parameter['value']
        print('{}已经修改为{}'.format(parameter['name'], context.k_value))
    if parameter['name'] == 'd值阀值':
        context.d_value = parameter['value']
        print('{}已经修改为{}'.format(parameter['name'], context.d_value))

def on_account_status(context, account):
    print(account)

if __name__ == '__main__':
    '''
    strategy_id策略ID,由系统生成
    filename文件名,请与本文件名保持一致
    mode实时模式:MODE_LIVE回测模式:MODE_BACKTEST
    token绑定计算机的ID,可在系统设置-密钥管理中生成
    backtest_start_time回测开始时间
    backtest_end_time回测结束时间
    backtest_adjust股票复权方式不复权:ADJUST_NONE前复权:ADJUST_PREV后复权:ADJUST_POST
    backtest_initial_cash回测初始资金
    backtest_commission_ratio回测佣金比例
    backtest_slippage_ratio回测滑点比例
    '''
    run(strategy_id='07c08563-a4a8-11ea-a682-7085c223669d', filename='main.py',
        mode=MODE_LIVE, token='2c4e3c59cde776ebc268bf6d7b4c457f204482b3',
        backtest_start_time='2020-09-01 08:00:00', backtest_end_time='2020-10-01 16:00:00',
        backtest_adjust=ADJUST_PREV, backtest_initial_cash=500000,
        backtest_commission_ratio=0.0001, backtest_slippage_ratio=0.0001)
```

## Level2 数据驱动事件示例

本示例提供 level2 行情的订阅，包括逐笔成交、逐笔委托、委托队列，仅券商托管版本支持。

```python
# coding=utf-8
from __future__ import print_function, absolute_import
from gm.api import *

def init(context):
    # 查询历史L2 Tick行情
    history_l2tick = get_history_l2ticks('SHSE.600519', '2020-11-23 14:00:00',
                                         '2020-11-23 15:00:00', fields=None, skip_suspended=True,
                                         fill_missing=None, adjust=ADJUST_NONE, adjust_end_time='', df=False)
    print(history_l2tick[0])

    # 查询历史L2 Bar行情
    history_l2bar = get_history_l2bars('SHSE.600000', '60s', '2020-11-23 14:00:00',
                                       '2020-11-23 15:00:00', fields=None, skip_suspended=True,
                                       fill_missing=None, adjust=ADJUST_NONE, adjust_end_time='', df=False)
    print(history_l2bar[0])

    # 查询历史L2 逐笔成交
    history_transactions = get_history_l2transactions('SHSE.600000', '2020-11-23 14:00:00',
                                                      '2020-11-23 15:00:00', fields=None, df=False)
    print(history_transactions[0])

    # 查询历史L2 逐笔委托
    history_order = get_history_l2orders('SZSE.000001', '2020-11-23 14:00:00',
                                         '2020-11-23 15:00:00', fields=None, df=False)
    print(history_order[0])

    # 订阅浦发银行的逐笔成交数据
    subscribe(symbols='SHSE.600000', frequency='l2transaction')
    # 订阅平安银行的逐笔委托数据
    subscribe(symbols='SZSE.000001', frequency='l2order')

def on_l2order(context, order):
    # 打印逐笔成交数据
    print(order)

def on_l2transaction(context, transition):
    # 打印逐笔委托数据
    print(transition)

if __name__ == '__main__':
    '''
    strategy_id策略ID, 由系统生成
    filename文件名, 请与本文件名保持一致
    mode运行模式, 实时模式:MODE_LIVE回测模式:MODE_BACKTEST
    token绑定计算机的ID, 可在系统设置-密钥管理中生成
    backtest_start_time回测开始时间
    backtest_end_time回测结束时间
    backtest_adjust股票复权方式, 不复权:ADJUST_NONE前复权:ADJUST_PREV后复权:ADJUST_POST
    backtest_initial_cash回测初始资金
    backtest_commission_ratio回测佣金比例
    backtest_slippage_ratio回测滑点比例
    '''
    run(strategy_id='strategy_id', filename='main.py', mode=MODE_BACKTEST,
        token='token_id', backtest_start_time='2020-11-01 08:00:00',
        backtest_end_time='2020-11-10 16:00:00', backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=10000000, backtest_commission_ratio=0.0001,
        backtest_slippage_ratio=0.0001)
```

## 可转债数据获取/交易示例

本示例提供可转债数据获取、可转债交易。

```python
# coding=utf-8
from __future__ import print_function, absolute_import
from gm.api import *

def init(context):
    # 获取可转债基本信息，输入可转债代码即可
    infos = get_symbol_infos(sec_type1=1030, symbols='SHSE.113038', df=True)

    # 输入可转债标的代码，可以获取到历史行情
    history_data = history(symbol='SHSE.113038', frequency='60s',
                           start_time='2021-02-24 14:50:00', end_time='2021-02-24 15:30:30',
                           adjust=ADJUST_PREV, df=True)

    # 可转债回售、转股、转股撤销，需要券商实盘环境，仿真回测不可用。
    # bond_convertible_call('SHSE.110051', 100, 0)
    # bond_convertible_put('SHSE.183350', 100)
    # bond_convertible_put_cancel('SHSE.183350', 100)

    # 可转债下单，仅将symbol替换为可转债标的代码即可
    order_volume(symbol='SZSE.128041', volume=100, side=OrderSide_Buy,
                 order_type=OrderType_Limit, position_effect=PositionEffect_Open, price=340)

    # 直接获取委托，可以看到相应的可转债委托，普通买卖通过标的体现可转债交易，转股、回售、回售撤销通过order_business字段的枚举值不同来体现。
    order_list = get_orders()

    # 订阅可转债行情。与股票无异
    subscribe(symbols='SHSE.113038', frequency='tick', count=2)

def on_tick(context, tick):
    # 打印频率为tick，可转债最新tick
    print(tick)

if __name__ == '__main__':
    run(strategy_id='strategy_id', filename='main.py', mode=MODE_LIVE,
        token='token_id', backtest_start_time='2020-12-16 09:00:00',
        backtest_end_time='2020-12-16 09:15:00', backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=10000000, backtest_commission_ratio=0.0001,
        backtest_slippage_ratio=0.0001)
```


# 策略程序架构

## 掘金策略程序初始化

通过[init 函数](/docs2/sdk/python/API介绍/基本函数.html#init-初始化策略)初始化策略，策略启动即会自动执行。在 init 函数中可以：

- **定义全局变量**：通过添加[context](/docs2/sdk/python/变量约定.html#context-上下文对象)包含的属性可以定义全局变量，如 `context.x`，该属性可以在全文中进行传递。
- **定义调度任务**：可以通过[schedule](/docs2/sdk/python/API介绍/基本函数.html#schedule-定时任务配置)配置定时任务，程序在指定时间自动执行策略算法。
- **准备历史数据**：通过[数据查询函数](/docs2/sdk/python/API介绍/通用数据函数（免费）.html#get-symbol-infos-查询标的基本信息)获取历史数据。
- **订阅实时行情**：通过[subscribe](/docs2/sdk/python/API介绍/数据订阅.html#subscribe-行情订阅)订阅行情，用以触发行情事件处理函数。

## 行情事件处理函数

- **处理盘口tick数据事件**：通过[on_tick](/docs2/sdk/python/API介绍/数据事件.html#on-tick-tick-数据推送事件)响应 tick 数据事件，可以在该函数中继续添加自己的策略逻辑，如进行数据计算、交易等。
- **处理分时bar数据事件**：通过[on_bar](/docs2/sdk/python/API介绍/数据事件.html#on-bar-bar-数据推送事件)响应 bar 数据事件，可以在该函数中继续添加自己的策略逻辑，如进行数据计算、交易等。

## 交易事件处理函数

- **处理回报 execrpt数据事件**：当交易委托被执行后会触发[on_execution_report](/docs2/sdk/python/API介绍/交易事件.html#on-execution-report-委托执行回报事件)，用于监测委托执行状态。
- **处理委托 order委托状态变化数据事件**：当[订单状态](/docs2/sdk/python/枚举常量.html#orderstatus委托状态)产生变化时会触发[on_order_status](/docs2/sdk/python/API介绍/交易事件.html#on-order-status-委托状态更新事件)，用于监测委托状态变更。
- **处理账户 account交易账户状态变化数据事件**：当[交易账户状态](/docs2/sdk/python/枚举常量.html#accountstatus交易账户状态)产生变化时会触发[on_account_status](/docs2/sdk/python/API介绍/交易事件.html#on-account-status-交易账户状态更新事件)，用于监测交易账户委托状态变更。

## 其他事件处理函数

- **处理 error错误事件**：当发生异常情况时触发[错误事件](/docs2/sdk/python/API介绍/其他事件.html#on-error-错误事件)，并返回[错误码和错误信息](/docs2/sdk/python/错误码.html#错误码)。
- **处理动态参数 parameter动态参数修改事件**：当[动态参数](/docs2/sdk/python/API介绍/动态参数.html#add-parameter-增加动态参数)产生变化时会触发[on_parameter](/docs2/sdk/python/API介绍/动态参数.html#on-parameter-动态参数修改事件推送)，用于监测动态参数修改。
- **处理绩效指标对象 Indicator回测结束事件**：在回测模式下，回测结束后会触发[on_backtest_finished](/docs2/sdk/python/API介绍/其他事件.html#on-backtest-finished-回测结束事件)，并返回回测得到的[绩效指标对象](/docs2/sdk/python/数据结构.html#indicator-绩效指标对象)。
- **处理实时行情网络连接成功事件**：当实时行情网络连接成功时触发[实时行情网络连接成功事件](/docs2/sdk/python/API介绍/其他事件.html#on-market-data-connected-实时行情网络连接成功事件)。


# 变量约定

## symbol - 代码标识

掘金代码(symbol)是掘金平台用于唯一标识交易标的代码，格式为：`交易所代码.交易标代码`，比如深圳平安的symbol，示例：`SZSE.000001`（注意区分大小写）。

板块为：`BK.板块代码`，比如鸿蒙概念的symbol，示例：`BK.007347`，板块symbol可通过`get_symbols(sec_type1=1070)`获取。

### 交易所代码

目前掘金支持国内的 8 个交易所，各交易所的代码缩写如下：

| 市场中文名 | 市场代码 |
|---|---|
| 上交所 | SHSE |
| 深交所 | SZSE |
| 中金所 | CFFEX |
| 上期所 | SHFE |
| 大商所 | DCE |
| 郑商所 | CZCE |
| 上海国际能源交易中心 | INE |
| 广期所 | GFEX |

### 交易标的代码

交易表代码是指交易所给出的交易标的代码，包括股票（如 600000）、期货（如 rb2011）、期权（如 10002498）、指数（如 000001）、基金（如 510300）等代码。具体的代码请参考交易所的给出的证券代码定义。

### symbol 示例

| 市场中文名 | 市场代码 | 示例代码 | 证券简称 |
|---|---|---|---|
| 上交所 | SHSE | SHSE.600000 | 浦发银行 |
| 深交所 | SZSE | SZSE.000001 | 平安银行 |
| 中金所 | CFFEX | CFFEX.IC2011 | 中证 500 指数 2020 年 11 月期货合约 |
| 上期所 | SHFE | SHFE.rb2011 | 螺纹钢 2020 年 11 月期货合约 |
| 大商所 | DCE | DCE.m2011 | 豆粕 2020 年 11 月期货合约 |
| 郑商所 | CZCE | CZCE.FG101 | 玻璃 2021 年 1 月期货合约 |
| 上海国际能源交易中心 | INE | INE.sc2011 | 原油 2020 年 11 月期货合约 |
| 广期所 | GFEX | GFEX.lc2405 | 碳酸锂 2024 年 05 月期货合约 |



### 虚拟合约

| 市场中文名 | 市场代码 | 示例代码 | 证券简称 |
|---|---|---|---|
| 上期所 | SHFE | SHFE.RB | 螺纹钢主力连续合约 |
| 上期所 | SHFE | SHFE.RB22 | 螺纹钢次主力连续合约 |
| 上期所 | SHFE | SHFE.RB99 | 螺纹钢加权指数合约 |
| 上期所 | SHFE | SHFE.RB00 | 螺纹钢当月连续合约 |
| 上期所 | SHFE | SHFE.RB01 | 螺纹钢下月连续合约 |
| 上期所 | SHFE | SHFE.RB02 | 螺纹钢下季连续合约 |
| 上期所 | SHFE | SHFE.RB03 | 螺纹钢隔季连续合约 |



### 期货主力连续合约

仅回测模式下使用，期货主力连续合约为量价数据的简单拼接，未做平滑处理，如 SHFE.RB 螺纹钢主力连续合约，其他主力合约请查看[期货主力连续合约](/docs2/docs/期货.html#连续合约数据)。

## mode - 模式选择

策略支持两种运行模式，需要在`run()`里面指定，分别为实时模式和回测模式。

### 实时模式

实时模式需指定 `mode = MODE_LIVE`，订阅行情服务器推送的实时行情，也就是交易所的实时行情，只在交易时段提供，常用于仿真和实盘。

### 回测模式

回测模式需指定 `mode = MODE_BACKTEST`。


# 数据结构

## 数据类

### Tick - Tick 对象

行情快照数据（包含盘口数据和当天动态日线数据）：

| 参数名 | 类型 | 说明 |
|---|---|---|
| symbol | str | [标的代码](/docs2/sdk/python/变量约定.html#symbol-代码标识) |
| open | float | 日线开盘价 |
| high | float | 日线最高价 |
| low | float | 日线最低价 |
| price | float | 最新价（集合竞价成交前price为0） |
| cum_volume | long | 最新总成交量，累计值（日线成交量） |
| cum_amount | float | 最新总成交额，累计值（日线成交金额） |
| cum_position | int | 合约持仓量（只适用于期货），累计值（股票此值为 0） |
| trade_type | int | 交易类型（只适用于期货）1: '双开', 2: '双平', 3: '多开', 4: '空开', 5: '空平', 6: '多平', 7: '多换', 8: '空换' |
| last_volume | int | 最新瞬时成交量 |
| last_amount | float | 最新瞬时成交额（郑商所不支持） |
| created_at | datetime.datetime | 创建时间 |
| quotes | [] (list of dict) | 股票提供买卖 5 档数据，list[0]~list[4]分别对应买卖一档到五档；期货提供买卖 1 档数据，list[0]表示买卖一档；level2 行情对应的是 list[0]~list[9]买卖一档到十档 |
| iopv | float | 基金份额参考净值（只适用于基金） |



#### 报价quote - (dict 类型)

| 参数名 | 类型 | 说明 |
|---|---|---|
| bid_p | float | 买价 |
| bid_v | int | 买量 |
| ask_p | float | 卖价 |
| ask_v | int | 卖量 |
| bid_q | dict | 委买队列，包含（total_orders（int）委托总个数，queue_volumes (list) 委托量队列），仅 level2 行情支持 |
| ask_q | dict | 委卖队列，包含（total_orders（int）委托总个数，queue_volumes (list) 委托量队列），仅 level2 行情支持 |



**注意**：
1. tick 是分笔成交数据，股票频率为 3s，期货为 0.5s，指数 5s，包含集合竞价数据，股票早盘集合竞价数为 09:15:00-09:25:00 的 tick 数据
2. 涨停时，没有卖价和卖量，ask_p 和 ask_v 用 0 填充；跌停时，没有买价和买量，bid_p 和 bid_v 用 0 填充
3. queue_volumes 委托量队列，只能获取到最优第一档的前 50 个委托量（不活跃标的可能会不足 50 个）

### Bar - Bar 对象

bar 数据是指各种频率的行情数据：

| 参数名 | 类型 | 说明 |
|---|---|---|
| symbol | str | [标的代码](/docs2/sdk/python/变量约定.html#symbol-代码标识) |
| frequency | str | 频率，支持 'tick', '60s', '300s', '900s' 等，默认'1d' |
| open | float | 开盘价 |
| close | float | 收盘价 |




# API介绍

## 基本函数

### init - 初始化策略

初始化策略，策略启动时自动执行。可以在这里初始化策略配置参数。

**函数原型**：`init(context)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文，全局变量可存储在这里 |



**示例**：
```python
def init(context):
    # 订阅bar
    subscribe(symbols='SHSE.600000,SHSE.600004', frequency='30s', count=5)
    # 增加对象属性，如：设置一个股票资金占用百分比
    context.percentage_stock = 0.8
```

**注意**：
1. 回测模式下 init 函数里不支持交易操作，仿真模式和实盘模式支持。
2. init 只会在策略启动时运行一次，如果不是每天重启策略，每天需要查询更新数据，可以通过设置定时任务执行。

### schedule - 定时任务配置

在指定时间自动执行策略算法，通常用于选股类型策略。

**函数原型**：`schedule(schedule_func, date_rule, time_rule)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| schedule_func | function | 策略定时执行算法 |
| date_rule | str | n + 时间单位，可选'd/w/m' 表示 n 天/n 周/n 月 |
| time_rule | str | 执行算法的具体时间（%H:%M:%S 格式） |



**示例**：
```python
def init(context):
    # 每天的19:06:20执行策略algo_1
    schedule(schedule_func=algo_1, date_rule='1d', time_rule='19:06:20')
    # 每月的第一个交易日的09:40:00执行策略algo_2
    schedule(schedule_func=algo_2, date_rule='1m', time_rule='9:40:00')

def algo_1(context):
    print(context.symbols)

def algo_2(context):
    order_volume(symbol='SHSE.600000', volume=200, side=OrderSide_Buy,
                 order_type=OrderType_Market, position_effect=PositionEffect_Open)
```

**注意**：
1. time_rule 的时、分、秒均不可以只输入个位数，例：'9:40:0'或'14:5:0'
2. 目前暂时支持1d、1w、1m，其中1w、1m仅用于回测

### run - 运行策略

**函数原型**：
```python
run(strategy_id='', filename='', mode=MODE_UNKNOWN, token='',
    backtest_start_time='', backtest_end_time='',
    backtest_initial_cash=1000000, backtest_transaction_ratio=1,
    backtest_commission_ratio=0, backtest_slippage_ratio=0,
    backtest_adjust=ADJUST_NONE, backtest_check_cache=1,
    serv_addr='', backtest_match_mode=0, backtest_intraday=0)
```

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| strategy_id | str | 策略 id |
| filename | str | 策略文件名称 |
| mode | int | 策略模式 MODE_LIVE(实时)=1 MODE_BACKTEST(回测)=2 |
| token | str | 用户标识 |
| backtest_start_time | str | 回测开始时间（%Y-%m-%d %H:%M:%S 格式） |
| backtest_end_time | str | 回测结束时间（%Y-%m-%d %H:%M:%S 格式） |
| backtest_initial_cash | double | 回测初始资金，默认 1000000 |
| backtest_transaction_ratio | double | 回测成交比例，默认 1.0，即下单 100%成交 |

## 数据订阅

### subscribe - 行情订阅

订阅行情，可以指定 symbol、数据滑窗大小，以及是否需要等待全部代码的数据到齐再触发事件。

**函数原型**：`subscribe(symbols, frequency='1d', count=1, unsubscribe_previous=False)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| symbols | str or list | 订阅[标的代码](/docs2/sdk/python/变量约定.html#symbol-代码标识)，注意大小写，支持字串格式，如有多个代码，中间用 ,（英文逗号）隔开，也支持 ['symbol1', 'symbol2'] 这种列表格式 |
| frequency | str | 频率，支持 'tick', '60s', '300s', '900s' 等，默认'1d' |
| count | int | context.data返回的订阅数据滑窗大小，默认1 |
| wait_group | bool | 是否等待同一频率的bar同时到齐（只支持bar频率），默认False不取消，输入True则同时等待同频率所有bar到齐再一次性返回 |
| wait_group_timeout | str | 等待超时时间，只有wait_group=True时生效，默认'10s' |
| unsubscribe_previous | bool | 是否取消过去订阅的symbols，默认False不取消，输入True则取消所有原来的订阅 |
| fields | str | context.data返回的对象字段，如有多个字段，中间用,隔开，默认所有 |
| format | str | context.data返回的数据格式，默认"df"："df": 数据框格式，返回dataframe（默认）；"row": 原始行式组织格式，返回list[dict]（当用户对性能有要求时，推荐使用此格式）；"col": 列式组织格式，返回dict |

**示例**：
```python
def init(context):
    # 同时订阅600519的tick数据和分钟数据
    subscribe(symbols='SHSE.600519', frequency='tick', count=2)
    subscribe(symbols='SHSE.600519', frequency='60s', count=2)

def on_tick(context,tick):
    print('收到tick行情---', tick)

def on_bar(context,bars):
    print('收到bar行情---', bars)
    data = context.data(symbol='SHSE.600519', frequency='60s', count=2)
    print('bar数据滑窗---', data)
```

**注意**：
1. subscribe 支持多次调用，支持同一标的不同频率订阅。订阅后的数据储存在本地，需要通过 context.data 接口调用或是直接在 on_tick 或 on_bar 中获取。
2. 在实时模式下，最新返回的数据是不复权的。
3. 订阅函数 subscribe 里面指定字段越少，查询速度越快，目前效率是 row > col > df。
4. 当 subscribe 的 format 指定 col 时，tick 的 quotes 字段会被拆分，只返回买卖一档的量和价，即只有 bid_p，bid_v，ask_p 和 ask_v。
5. 在回测模式下，subscribe 使用 wait_group=True 时，等待的标的需要下个时间到期。

## 数据事件

数据事件是阻塞回调事件函数，通过 subscribe 函数订阅，主动推送。

### on_tick - tick 数据推送事件

接收 tick 分笔数据，通过 subscribe 订阅 tick 行情，行情服务主动推送 tick 数据。

**函数原型**：`on_tick(context, tick)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context 对象](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文 |
| tick | [tick 对象](/docs2/sdk/python/数据结构.html#tick-tick-对象) | 当前被推送的 tick |

**示例**：
```python
def init(context):
    # 订阅600519的tick数据
    subscribe(symbols='SHSE.600519', frequency='tick', count=2)

def on_tick(context,tick):
    print('收到tick行情---', tick)
```

输出：
```python
{'symbol': 'SHSE.600519', 'created_at': datetime.datetime(2020, 9, 2, 14, 7, 23, 620000, tzinfo=tzfile('PRC')), 'price': 1798.8800048828125, 'open': 1825.0, 'high': 1828.0, 'low': 1770.0, 'cum_volume': 2651191, 'cum_amount': 4760586491.0, 'cum_position': 0, 'last_amount': 179888.0, 'last_volume': 100, 'trade_type': 0, 'receive_local_time': 1602751345.262745}
```

### on_bar - bar 数据推送事件

接收固定周期 bar 数据，通过 subscribe 订阅 bar 行情，行情服务主动推送 bar 数据。

**函数原型**：`on_bar(context, bars)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context 对象](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文对象 |
| bars | list([bar](/docs2/sdk/python/数据结构.html#Bar-Bar对象)) | 当前被推送的 bar 列表 |

**示例**：
```python
# coding=utf-8
from __future__ import print_function, absolute_import
from gm.api import *
from datetime import datetime, timedelta

def init(context):
    # 订阅600519和000001的分钟数据
    subscribe(symbols='SHSE.600519,SZSE.000001', frequency='60s', count=2)

def on_bar(context,bars):
    print('收到bars行情---', bars)

if __name__ == '__main__':
    '''
    strategy_id策略ID, 由系统生成
    filename文件名, 请与本文件名保持一致
    mode运行模式, 实时模式:MODE_LIVE回测模式:MODE_BACKTEST
    token绑定计算机的ID, 可在系统设置-密钥管理中生成
    backtest_start_time回测开始时间
    backtest_end_time回测结束时间
    backtest_adjust股票复权方式, 不复权:ADJUST_NONE前复权:ADJUST_PREV后复权:ADJUST_POST
    backtest_initial_cash回测初始资金
    backtest_commission_ratio回测佣金比例
    backtest_slippage_ratio回测滑点比例
    backtest_match_mode市价撮合模式，以下一tick/bar开盘价撮合:0，以当前tick/bar收盘价撮合：1
    '''
    backtest_start_time = str(datetime.now() - timedelta(weeks=1))[:19]
    backtest_end_time = str(datetime.now())[:19]
    run(strategy_id='xxxxxx', filename='main.py', mode=MODE_BACKTEST,
        token='xxxxxxx', backtest_start_time=backtest_start_time,
        backtest_end_time=backtest_end_time,
```

## 交易事件

### on_order_status - 委托状态更新事件

响应委托状态更新事件，下单后及委托状态更新时被触发。

**注意**：
1. 撤单拒绝，会推送撤单委托的最终状态。
2. 回测模式下，交易事件顺序与实时模式一致，委托状态待报和已报是虚拟状态，不会更新持仓和资金。已成状态后才会更新持仓和资金。

**函数原型**：`on_order_status(context, order)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文 |
| order | [order 对象](/docs2/sdk/python/数据结构.html#order-委托对象) | 委托 |

**示例1**：
```python
def on_order_status(context, order):
    print(order)
```

输出：
```python
{'strategy_id': 'd7443a53-f65b-11ea-bb9d-484d7eaefe55', 'account_id': 'd7443a53-f65b-11ea-bb9d-484d7eaefe55', 'cl_ord_id': '000000000', 'symbol': 'SHSE.600000', 'side': 1, 'position_effect': 1, 'position_side': 1, 'order_type': 1, 'status': 3, 'price': 11.0, 'order_style': 1, 'volume': 18181800, 'value': 200000000.0, 'percent': 0.1, 'target_volume': 18181800, 'target_value': 199999800.0, 'target_percent': 0.1, 'filled_volume': 18181800, 'filled_vwap': 11.0011, 'filled_amount': 200019799.98, 'created_at': datetime.datetime(2020, 9, 1, 9, 40, tzinfo=tzfile('PRC')), 'updated_at': datetime.datetime(2020, 9, 1, 9, 40, tzinfo=tzfile('PRC')), 'filled_commission': 20001.979998, 'account_name': '', 'order_id': '', 'ex_ord_id': '', 'algo_order_id': '', 'order_business': 0, 'order_duration': 0, 'order_qualifier': 0, 'order_src': 0, 'position_src': 0, 'ord_rej_reason': 0, 'ord_rej_reason_detail': '', 'stop_price': 0.0}
```

**示例2**：
```python
def init(context):
    # 记录委托id
    context.cl_ord_id = {}
    order_list = get_orders()
    context.cl_ord_id = {i['cl_ord_id']: {'status': i['status'], 'filled_volume': i['filled_volume']} for i in order_list if order_list}

def on_bar(context, bars):
    # 记录下单后对应的委托id，方便在on_order_status里追踪
    order = order_volume(symbol=symbol, volume=volume, side=OrderSide_Sell,
                         order_type=OrderType_Limit, position_effect=PositionEffect_CloseToday, price=price_1)
    context.cl_ord_id[order[0]['cl_ord_id']] = {}
    context.cl_ord_id[order[0]['cl_ord_id']]['status'] = order[0]['status']
    context.cl_ord_id[order[0]['cl_ord_id']]['filled_volume'] = order[0]['filled_volume']

def on_order_status(context, order):
    # ...
```

## 其他事件

### on_backtest_finished - 回测结束事件

在回测模式下，回测结束后会触发该事件，并返回回测得到的绩效指标对象。

**函数原型**：`on_backtest_finished(context, indicator)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文 |
| indicator | [indicator](/docs2/sdk/python/数据结构.html#indicator-绩效指标对象) | 绩效指标 |

**示例**：
```python
def on_backtest_finished(context, indicator):
    print(indicator)
```

返回：
```python
{'account_id': 'd7443a53-f65b-11ea-bb9d-484d7eaefe55', 'pnl_ratio': -0.007426408687162637, 'pnl_ratio_annual': -1.3553195854071813, 'sharp_ratio': -15.034348187048744, 'max_drawdown': 0.0009580714324989177, 'risk_ratio': 0.10010591267452242, 'open_count': 1, 'close_count': 1, 'lose_count': 1, 'calmar_ratio': -1414.6331259164358, 'win_count': 0, 'win_ratio': 0.0, 'created_at': None, 'updated_at': None}
```

### on_error - 错误事件

当发生异常情况，比如断网时、终端服务崩溃时触发。

**函数原型**：`on_error(context, code, info)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文 |
| code | int | [错误码](/docs2/sdk/python/错误码.html) |
| info | str | 错误信息 |

**示例**：
```python
def on_error(context, code, info):
    print('code:{}, info:{}'.format(code, info))
    stop()
```

返回：`code:1201, info:实时行情服务连接断开`

### on_market_data_connected - 实时行情网络连接成功事件

实时行情网络连接时触发，比如策略实时运行启动后会触发、行情断连又重连后会触发。

**函数原型**：`on_market_data_connected(context)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文 |

**示例**：
```python
def on_market_data_connected(context):
    print('实时行情网络连接成功')
```

### on_trade_data_connected - 交易通道网络连接成功事件

目前监控 SDK 的交易和终端的链接情况，终端之后部分暂未做在内。账号连接情况可通过终端内账户连接指示灯查看。

**函数原型**：`on_trade_data_connected(context)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文 |

**示例**：
```python
def on_trade_data_connected(context):
    print('交易通道网络连接成功')
```

### on_market_data_disconnected - 实时行情网络连接断开事件

实时行情网络断开时触发，比如策略实时运行行情断连会触发。

**函数原型**：`on_market_data_disconnected(context)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文 |

## 动态参数

动态参数仅在仿真交易和实盘交易下生效，可在终端设置和修改。动态参数通过策略调用接口实现策略和掘金界面参数交互，在不停止策略运行的情况下，界面修改参数（移开光标，修改就会生效）会对策略里的指定变量做修改。

### add_parameter - 增加动态参数

**函数原型**：`add_parameter(key, value, min=0, max=0, name='', intro='', group='', readonly=False)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| key | str | 参数的键 |
| value | double | 参数的值 |
| min | double | 最小值 |
| max | double | 最大值 |
| name | str | 参数名称 |
| intro | str | 参数说明 |
| group | str | 参数的组 |
| readonly | bool | 是否为只读参数 |

**示例**：
```python
context.k_value = 80
add_parameter(key='k_value', value=context.k_value, min=0, max=100,
              name='k值阀值', intro='调整k值', group='1', readonly=False)
```

### set_parameter - 修改已经添加过的动态参数

**注意**：需要保持 key 键名和添加过的动态参数的 key 一致，否则不生效，无报错。

**函数原型**：`set_parameter(key, value, min=0, max=0, name='', intro='', group='', readonly=False)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| key | str | 参数的键 |
| value | double | 参数的值 |
| min | double | 最小值 |
| max | double | 最大值 |
| name | str | 参数名称 |
| intro | str | 参数说明 |
| group | str | 参数的组 |
| readonly | bool | 是否为只读参数 |

**示例**：
```python
context.k_xl = 0.3
set_parameter(key='k_value', value=context.k_xl, min=0, max=1,
              name='k值斜率', intro='调整k值斜率', group='1', readonly=False)
```

### on_parameter - 动态参数修改事件推送

**函数原型**：`on_parameter(context, parameter)`

**参数**：

| 参数名 | 类型 | 说明 |
|---|---|---|
| context | [context](/docs2/sdk/python/变量约定.html#context-上下文对象) | 上下文 |
| parameter | dict | 当前被推送的动态参数对象 |

**示例**：
```python
def on_parameter(context, parameter):
    print(parameter)
```

输出：
```python
{'key': 'k_value', 'value': 80.0, 'max': 100.0, 'name': 'k值阀值', 'intro': '调整k值', 'group': '1', 'min': 0.0, 'readonly': False}
```

### context.parameters - 获取所有动态参数

返回数据类型为字典，key 为动态参数的 key，值为动态参数对象。

**示例**：
```python
print(context.parameters)
```

输出：
```python
{'k_value': {'key': 'k_value', 'value': 80.0, 'max': 100.0, 'name': 'k值阀值', 'intro': 'k值阀值', 'group': '1', 'min': 0.0, 'readonly': False}, 'd_value': {'key': 'd_value', 'value': 20.0, 'max': 100.0, 'name': 'd值阀值', 'intro': 'd值阀值', 'group': '1', 'min': 0.0, 'readonly': False}}
```

## 通用数据函数（免费）

python 通用数据 API 包含在 gm3.0.148 版本及以上版本，不需要引入新库。

### get_symbol_infos - 查询标的基本信息

获取指定（范围）交易标的基本信息，与时间无关。此函数为掘金公版（体验版/专业版/机构版）函数，券商版以升级提示为准。

**函数原型**：
```python
get_symbol_infos(sec_type1, sec_type2=None, exchanges=None, symbols=None, df=False)
```

**参数**：

| 参数名 | 类型 | 中文名称 | 必填 | 默认值 | 参数用法说明 |
|---|---|---|---|---|---|
| sec_type1 | int | 证券品种大类 | Y | 无 | 指定一种证券大类，只能输入一个。证券大类 sec_type1 清单：1010: 股票，1020: 基金，1030: 债券，1040: 期货，1050: 期权，1060: 指数，1070：板块 |
| sec_type2 | int | 证券品种细类 | N | None | 指定一种证券细类，只能输入一个。默认None表示不区分细类 |
| exchanges | str or list | 交易所代码 | N | None | 输入交易所代码，可输入多个。默认None表示所有交易所 |
| symbols | str or list | 标的代码 | N | None | 输入标的代码，可输入多个。默认None表示所有标的 |
| df | bool | 返回格式 | N | False | 是否返回 dataframe 格式，默认False返回字典格式 |

**证券细类 sec_type2 清单**：
- **股票**：101001:A 股，101002:B 股，101003:存托凭证
- **基金**：102001:ETF，102002:LOF，102005:FOF，102009:基础设施REITs
- **债券**：103001:可转债，103008:回购
- **期货**：104001:股指期货，104003:商品期货，104006:国债期货
- **期权**：105001:股票期权，105002:指数期权，105003:商品期权
- **指数**：106001:股票指数，106002:基金指数，106003:债券指数，106004:期货指数
- **板块**：107001:概念板块

**交易所代码清单**：SHSE（上海证券交易所），SZSE（深圳证券交易所），CFFEX（中金所），SHFE（上期所），DCE（大商所），CZCE（郑商所），INE（上海国际能源交易中心），GFEX（广期所）

**返回值字段**：

| 字段名 | 类型 | 中文名称 | 说明 |
|---|---|---|---|
| symbol | str | 标的代码 | exchange.sec_id |
| sec_type1 | int | 证券品种大类 | 1010: 股票，1020: 基金，1030: 债券，1040: 期货，1050: 期权，1060: 指数，1070：板块 |
| sec_type2 | int | 证券品种细类 | — |

（股票/基金/债券/期货/期权/指数均支持）


# 枚举常量

## OrderStatus委托状态

| 枚举值 | 说明 |
|---|---|
| OrderStatus_New = 1 | 已报 |
| OrderStatus_PartiallyFilled = 2 | 部成 |
| OrderStatus_Filled = 3 | 已成 |
| OrderStatus_Canceled = 5 | 已撤 |
| OrderStatus_Rejected = 8 | 已拒绝 |
| OrderStatus_PendingNew = 10 | 待报 |
| OrderStatus_Expired = 12 | 已过期 |
| OrderStatus_PendingTrigger = 15 | 待触发，CTP条件单 |
| OrderStatus_Triggered = 16 | 已触发，CTP条件单 |



## OrderSide委托方向

| 枚举值 | 说明 |
|---|---|
| OrderSide_Buy = 1 | 买入 |
| OrderSide_Sell = 2 | 卖出 |



## OrderType委托类型

用于映射OrderDuration和OrderQualifier的参数组合，推荐下单时直接指定OrderType，可无需额外指定OrderDuration和OrderQualifier。

**通用**：
- `OrderType_Limit = 1`：限价委托（全部交易所支持）
- `OrderType_Market = 2`：市价委托（上期所和上能所不支持，中金所远期合约不支持，可转债不支持，上交所需要填上price保护限价）

**上交所（终端3.18.0.0以上新增）**：
- `OrderType_Limit = 1`：限价
- `OrderType_Market = 2`：市价（默认五档即成转限）
- `OrderType_Market_BOC = 20`：市价对方最优价格（best of counterparty）
- `OrderType_Market_BOP = 21`：市价己方最优价格（best of party）
- `OrderType_Market_B5TC = 24`：市价最优五档剩余撤销（best 5 then cancel）
- `OrderType_Market_B5TL = 25`：市价最优五档剩余转限价（best 5 then limit）

**深交所**：
- `OrderType_Limit = 1`：限价
- `OrderType_Market = 2`：市价（默认对方最优价）
- `OrderType_Market_BOC = 20`：市价对方最优价格（best of counterparty）
- `OrderType_Market_BOP = 21`：市价己方最优价格（best of party）
- `OrderType_Market_FAK = 22`：市价即时成交剩余撤销（fill and kill）
- `OrderType_Market_FOK = 23`：市价即时全额成交或撤销（fill or kill）
- `OrderType_Market_B5TC = 24`：市价最优五档剩余撤销（best 5 then cancel）

**大商所**：
- `OrderType_Limit = 1`：限价
- `OrderType_Limit_FAK = 10`：限价即时成交剩余撤销（fill and kill）
- `OrderType_Limit_FOK = 11`：限价即时全额成交或撤销（fill or kill）
- `OrderType_Market = 2`：市价
- `OrderType_Market_FAK = 22`：市价即时成交剩余撤销（fill and kill）
- `OrderType_Market_FOK = 23`：市价即时全额成交或撤销（fill or kill）

**郑商所**：
- `OrderType_Limit = 1`：限价
- `OrderType_Market = 2`：市价
- `OrderType_Market_FOK = 23`：市价即时全额成交或撤销（fill or kill）

**上期所和上能所**：
- `OrderType_Limit = 1`：限价
- `OrderType_Limit_FAK = 10`：限价即时成交剩余撤销（fill and kill）
- `OrderType_Limit_FOK = 11`：限价即时全额成交或撤销（fill or kill）

**中金所**：
- `OrderType_Limit = 1`：限价
- `OrderType_Limit_FAK = 10`：限价即时成交剩余撤销（fill and kill）
- `OrderType_Limit_FOK = 11`：限价即时全额成交或撤销（fill or kill）
- `OrderType_Market_B5TC = 24`：市价最优五档剩余撤销（best 5 then cancel）


# 错误码

| 错误码 | 描述 | 解决方法 |
|---|---|---|
| 0 | 成功 | — |
| 1000 | 错误或无效的 token | 检查下[token](/docs2/sdk/python/API介绍/其他函数.html#set-token-设置-token)是否有误 |
| 1001 | 无法连接到终端服务 | 检查是否开启了掘金终端 |
| 1010 | 无法获取掘金服务器地址列表 | 检查是否开启了掘金终端 |
| 1013 | 交易服务调用错误 | 检查终端是否正常或重启掘金终端 |
| 1014 | 历史行情服务调用错误 | 在微信群或者 QQ 群通知技术支持 |
| 1015 | 策略服务调用错误 | 检查终端是否正常或重启掘金终端 |
| 1016 | 动态参数调用错误 | 检查[动态参数](/docs2/sdk/python/API介绍/动态参数.html#动态参数)设置 |
| 1017 | 基本面数据服务调用错误 | 在微信群或者 QQ 群通知技术支持 |
| 1018 | 回测服务调用错误 | 重启掘金终端、重新运行策略 |
| 1019 | 交易网关服务调用错误 | 检查终端是否正常或重启掘金终端 |
| 1020 | 无效的 ACCOUNT_ID | 检查账户 id 是否填写正确 |
| 1021 | 非法日期格式 | 对照帮助文档修改日期格式，检查 run()回测日期是否正确 |
| 1025 | 无法连接到认证服务 | 在微信群或者 QQ 群通知技术支持 |
| 1026 | 更新令牌错误 | 在微信群或者 QQ 群通知技术支持 |
| 1027 | 接口调用错误，无效入参 | 例如检查定时任务的频率参数，实时模式只支持 1d |
| 1028 | 不支持的服务 | 在微信群或者 QQ 群通知技术支持 |
| 1029 | 超出最大限制设置 | 检查入参的标的个数和时间范围 |
| 1100 | 交易消息服务连接失败 | 检查终端是否正常或重启掘金终端 |
| 1101 | 交易消息服务断开 | 一般不用处理，等待自动重连 |
| 1200 | 实时行情服务连接失败 | 一般不用处理，等待自动重连 |
| 1201 | 实时行情服务连接断开 | 一般不用处理，等待自动重连 |
| 1202 | 实时行情订阅失败 | 订阅代码标的数量超过账户权限，联系商务咨询权限 |
| 1300 | 初始化回测失败 | 检查终端是否启动或策略是否连接到终端 |
| 1301 | 回测时间区间错误 | 检查回测时间是否超出范围 |
| 1302 | 回测读取缓存数据错误 | 在微信群或者 QQ 群通知技术支持 |
| 1303 | 回测写入缓存数据错误 | 在微信群或者 QQ 群通知技术支持 |
| 2001 | 用户无此数据接口权限 | 联系商务咨询权限 |
| 2002 | 超出业务授权范围 | 调整数据日期范围，或者联系商务延长权限 |
| 2003 | 实时行情订阅代码数量超过用户权限 | 联系商务咨询权限 |
| 3001 | 超出数据接口调用频率限制（流控） | 检查程序是否异常运行导致循环取数，增加等待时间再调用 |

