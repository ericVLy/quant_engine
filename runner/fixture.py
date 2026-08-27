class MarketDataFixture:
    """Provide SDK-backed market data context for Case execution."""

    def __init__(self, broker):
        self.broker = broker

    def load(self, symbol, frequency='1d', count=50, fields=None, end_time=None):
        return self.broker.history_n(
            symbol=symbol, frequency=frequency, count=count,
            end_time=end_time, fields=fields, data_frame=True,
        )

    def subscribe(self, symbol, frequency='1d', count=1, fields=None):
        return self.broker.subscribe(
            symbols=symbol, frequency=frequency, count=count, fields=fields,
        )