"""
Stock bot risk package.

correlation.py: returns-based correlation gate for the rule-trading BUY path
— blocks a new position when it's highly correlated with an already-open
one. Reuses the crypto bot's pure Pearson/returns math (bot/risk/correlation.py).
"""
