# Hybrid strategy 14

Combines strategy 10's momentum/target controller with strategy 13's jump-following
formula and provisional pair map. Preserves both source strategies. Only one runner
and one connection are used; the strategy 13 order loop is never started.

Confirmed 5/20-second momentum builds toward +/-100 in clips of 5. Additionally,
a >=5-tick move in about one second can activate a linked follower that has not
yet caught up. Expected follower movement is beta times leader return, converted
to follower ticks; already completed movement is subtracted. Remaining movement
must clear 3 ticks and the cost/spread hurdle. Recheck the remaining movement on
each follower update; expire support after 10 seconds. Conflicting signals abstain.
The signal is a hypothesis, not a calibrated forecast. Queue/partial-fill risks
remain; entries and exits are passive through strategy 10.

The initial map is imported from strategy 13, not inferred from stock names.
It is provisional; --no-priors disables it. New co-jump links need two consistent
observations around the 10-second evaluation horizon before use. Strategy 13's
quiet-return correlation learner, IOC chasing, manual bets and PnL locks are not
carried into this hybrid. Existing strategy 10 freshness, reservations, target
checks, close-before-reverse and session wind-down apply.

Exact cloud command, after stopping the previous team bot:

```bash
cd /home/workspace/your_optiver_workspace/OptiverHackathon
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#14.py' run --live
```

Concrete defaults: all eight listed stocks, cap/target 100, clip 5, 600 seconds,
zero assumed round-trip fees, local maximum 25 client calls/second, maximum order
size 5. These are chosen settings, not verified exchange fees or request limits.
The run is dry by default without --live. The operator controls execution. Actual
portfolio rules and source timestamps still need the execution owner's verification.
No live connection was opened during development. Profitability is unverified.

Offline checks:

```bash
/opt/venv/bin/python 'optiver_bot/strategies/market_maker#14.py' demo
/opt/venv/bin/python -m unittest discover -s tests -p 'test_hybrid14.py' -v
/opt/venv/bin/python -m unittest discover -s tests -p 'test_target_position_10.py' -v
```
