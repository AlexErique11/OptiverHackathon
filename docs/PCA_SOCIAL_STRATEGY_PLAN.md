**Plan: combine PCA/SVD and social-feed analysis with the current wall-based market maker**

Prepared 12 November 2026. Planning only; no trading logic or live process changed.

Review scope: the original plan reviewed strategy #3 at `e609c51`. While preparing this document for main, `origin/main` advanced to `d0467d0`, adding strategy #4. Its diff adds centralized social-feed polling/logging and a configurable pause after any post (20 seconds by default). Preserve #4 and include its existing news-pause behavior as a baseline. The combined model proposed here extends that behavior with relevance, sentiment and PCA; it is not yet implemented.

The proposed candidate keeps the useful quoting mechanics of strategy #3, adds a shared view of stock movements plus social-feed information from `poll_new_social_media_feeds()`, and uses their combined evidence first to control risk and then, if validated, to tilt quotes toward predicted returns. Its objective is better performance after costs and liquidation, with controlled drawdown. Neither PCA nor social sentiment alone establishes that a crash or rally is coming.

1. **Freeze the actual baseline and resolve the inputs.**

   The strategy originally reviewed was `optiver_bot/strategies/market_maker#3.py`, introduced at `e609c51` and present in checkout `518929fb2ac16b2f378642e7ab919bc4dfe2c9c6`. Its CLI defaults to `decide_touch`, which uses the midpoint between qualifying bid/ask depth levels. The default wall threshold is 500 lots; the default half-spread is still 2 ticks. Strategy #4 at `d0467d0` retains these quoting mechanics and adds the news pause described above. These are code settings, not event rules or established optimums. Record the actual deployed source hash and arguments before comparing with live results; local latest does not prove deployed latest.

   Preserve own-order subtraction, inventory skew, passive tick-aligned prices, price-band checks, empty/crossed-book abstention, and retention of unchanged or partially filled orders. Preserve the baseline files unchanged. Implement on a separate branch, proposed `codex/pca-market-maker`, with a separate strategy module. This authored plan is versioned in `docs/`; datasets, feed contents, logs, fitted model artifacts and performance outputs remain in ignored directories. Implementation should use a separate worktree if the shared checkout is active.

   Treat wall midpoint as a candidate local reference. Order size does not prove that a level belongs to the exchange or represents fair value. Measure wall persistence, disappearance, asymmetry, and subsequent markouts. Compare wall midpoint with cleaned midpoint. Missing walls must not silently switch an existing price-history series to a different definition.

   `OPTIVER_HACKATHON_BRIEF.md` is preparation material and explicitly says the actual objective was unavailable. Before integration, read the unlocked brief and record the permitted universe, scoring, fees/rebates, request accounting, order limits, final inventory treatment, and allowed libraries. The confirmed per-instrument hard limit is -100 to +100. Other numerical settings below are research candidates, not exchange rules.

2. **Repair the data/execution prerequisites before adding directional exposure.**

   The existing adapter discards book timestamps. The runner reads positions once per iteration and may insert replacements after cancellation without refreshing positions and outstanding orders. Existing local review artifacts reproduce positions +61/-61 with internal cap 60 during a cancellation/fill race; that example does not breach the exchange hard cap, but exposes a missing invariant. The current sleep is pacing, not verified team-wide request-budget enforcement. Existing CSV output lacks synchronized depth/history needed for this model.

   Add timestamp preservation, stale-book checks, explicit unknown-state handling, budget accounting, and cancellation/fill reconciliation in the one shared execution owner. Reserve pending inserts and the full remaining quantity of orders awaiting cancellation. Confirm fresh positions/order state before releasing reservations or sending replacements. Include an inventory reduction and session-end policy; returning no quotes cancels targets but does not liquidate inventory.

   The original strategy and new candidate must eventually run through the same corrected execution/risk layer for a fair comparison. Preserve the original source for reference rather than using its execution gap to make the candidate look better.

3. **Collect a causal, synchronized stock panel.**

   Use the existing process's observations. An offline researcher may read saved records; it must not open another exchange connection. Persist receive timestamps, exchange timestamps where available, raw and cleaned depth, tick sizes, own orders/fills/positions, public trades if available, raw social-feed events and their availability times, request outcomes and model versions in ignored directories. Respect the actual API budget.

   Start with the confirmed tradable stock universe. Here, “market” means that basket of simulated stocks; it does not mean the entire real-world stock market. Fix stock ordering and universe membership within each model version.

   Build a common time grid using only observations available by each cutoff. A 1-second grid is an initial candidate, conditional on measured update frequency, timestamp quality and budget. Reject stale/crossed/missing books; do not interpolate from the future or forward-fill indefinitely. Explicitly track each stock's age and the panel's synchronization quality. Insufficient coverage disables the factor overlay rather than treating missing returns as zero. A changed universe requires a new model version and warm-up.

   Use cleaned midpoint log returns as the first PCA input: `r[t,i] = log(mid[t,i] / mid[t-1,i])`. Keep wall-reference changes, spread, depth and imbalance as separate features. Do not apply PCA directly to stock price levels. Investigate zero-variance columns and timestamp artifacts. A single saved visualizer page or short public-trade extract is insufficient for this panel; the inspected data directory currently contains a synthetic replay, not a suitable recorded history.

4. **Collect and interpret social feeds through the existing connection.**

   Have the one execution/data owner call `exchange.poll_new_social_media_feeds()` at a cadence permitted by the confirmed request budget. Poll centrally, persist the returned batch, and distribute the same immutable events to all consumers; strategy modules must not each poll independently. Verify whether polling consumes a shared unread buffer, what history is retained, and how reconnection behaves in the supplied cloud reference. This plan does not initiate a connection or poll the running account.

   Strategy #4 already calls this API from `ExchangeAdapter.poll_feed()`. Extend that existing collection path instead of adding another poller. Its adapter currently converts `timestamp`, `post` and `meta_data` to strings and maps exceptions to an empty list. These are implementation assumptions pending API verification; preserve structured metadata when supplied and distinguish failures from successful empty polls. Its news guard requests bulk cancellations without using their returned status; retain cancellation reservations until fresh order/position reconciliation confirms the outcome. The fixed pause is a comparison control, not evidence that 20 seconds is optimal.

   The local preparation brief lists this method, but the public event API pages were inaccessible during this update. Before coding the adapter, read `/home/workspace/api_reference.md` and inspect already available feed records through the existing owner. Confirm the actual return type, text fields, source/author identifiers, event identifiers, timestamps, instrument references, ordering and empty/error behavior. The normalized fields described here are our proposed schema, not claims about fields returned by the API. If the challenge provides a defined signal or documented news-to-value relationship, implement that specification before considering generic sentiment.

   Preserve raw text locally alongside a normalized event ID, text, optional source/published time, receive time, processing-complete time, resolved instruments, relevance and extraction quality. Deduplicate stable IDs when available; otherwise use a documented hash of stable event content and metadata, without collapsing distinct later events merely because their text matches. Handle duplicates across polls, history reads and restarts. Keep revisions as new information with their own first-seen time. Do not assume a historical getter supplies exchange-wide pre-connection history.

   Make an event eligible only once it was received and its features were ready. Keep publication time for age/latency diagnostics, but never backdate a late message to that time in replay. If backfill availability is unknown, exclude it from predictive evaluation or mark it available at ingestion. Distinguish a successful empty poll from an API error or feed outage. Log feed health independently of message count; a quiet stream is not automatically stale.

   Start with a deterministic, versioned text parser or locally available approved fixed model. Resolve tickers/company aliases against the confirmed universe, distinguish company-specific from basket-wide messages, and allow ambiguous/unrelated messages to abstain. Extract signed sentiment, negative-event flags, relevance, novelty, message counts and age. Test negation and conflicting messages. Extraction confidence is not a calibrated probability of a future market move. Avoid unvalidated author influence scores. Treat message text as data; it cannot change code, limits or execution instructions. Keep feed content in ignored local storage rather than sending it to an external service by default.

   Aggregate decaying per-stock sentiment with bounded contributions, for example `S_i(t) = sum(relevance * extraction_quality * signed_score * exp(-age/tau)) / (epsilon + sum(relevance * extraction_quality * exp(-age/tau)))`. Use only events available by `t`; expire old messages, and retain separate counts/coverage so one weak post is distinguishable from several independent reports. Select decay and maximum age on training/validation data. Include broad-market sentiment separately rather than copying one company event into every stock.

   Deliver an immutable `SocialContext` containing per-stock sentiment, broad sentiment, negative-event counts, novelty, coverage, newest eligible event time, extraction version and feed health. After an outage, mark context uncertain and fall back to the separately validated PCA-only path. Do not interpret missing feed data as positive, negative or verified neutral news.

5. **Compute and interpret PCA using SVD.**

   Construct a time-by-stock return matrix. Center and scale each column using only its fitting window. Use scale floors and abstain on degenerate inputs. Scaling is explicit: standard PCA implementations center data but do not necessarily scale it. Compute deterministic full SVD for this small universe:

   ```text
   X = U @ diag(s) @ V.T
   stock loadings = columns of V
   historical factor scores = X @ V = U @ diag(s)
   eigenvalue[k] = s[k]^2 / (number_of_rows - 1)
   explained_variance_share[k] = s[k]^2 / sum(s^2)
   new factor score = standardized_new_return @ V
   ```

   PCA is the statistical decomposition; SVD is the numerical method used to calculate it. This is one coherent model, not two independent votes. See the [PCA documentation](https://scikit-learn.org/stable/modules/generated/sklearn.decomposition.PCA.html) and [NumPy SVD documentation](https://numpy.org/doc/stable/reference/generated/numpy.linalg.svd.html).

   Begin with one component and evaluate two or three only if validation supports them. A component with broad, similarly signed stock loadings may represent shared basket movement. A component concentrated in one name or with mixed signs must not automatically be called the market. Interpret later components from their actual stock contributions; do not assign sector names in advance.

   Orient a broad component consistently against an equal-weight basket. Use a deterministic tie rule and mark weak alignment as ambiguous. Monitor loading changes, eigenvalue separation, component swaps and subspace stability. Sign alignment alone cannot fix rotations among nearly equal components. Freeze the scaler, loadings and predictor together between scheduled refits; refit them together using only matured past labels. Avoid feeding scores from a newly rotated basis into old prediction coefficients.

   Evaluate a small set of trailing fit windows, such as 5 versus 15 minutes, only when recordings support them. These windows establish a factor estimate, not enough evidence by themselves to train or validate a crash predictor. Choose refit cadence from timing measurements, not on every order update. Pin numerical dependency versions and log model/data hashes; use no randomized solver in the first version.

6. **Combine PCA and social features to predict future moves.**

   Define a fixed equal-weight basket index from normalized stock prices, independent of the PCA weights. That gives prediction targets consistent meaning even when the PCA basis changes. Select one primary horizon based on observed execution/holding times; 15 seconds is a candidate, with 5 and 60 seconds reserved as limited sensitivity checks.

   The direction target is the basket's future return from decision time to the selected horizon. Define a sharp-decline event separately: the basket falls below a threshold at any point during that horizon. A decline followed by recovery can therefore be both a tail event and a positive endpoint return. Choose the decline threshold and any volatility normalization using training data; freeze the definition before testing. The threshold should describe a material move, not bid/ask noise.

   Start with a small price feature set: recent signed factor returns, factor volatility, first-component variance share and its change, fraction of stocks falling, and basket spread/depth deterioration. Add social features from the same causal cutoff: broad sentiment, negative-event intensity, novelty, coverage and feed health. Keep PCA fitted on stock returns in the first version; combine its scores with text-derived features downstream rather than mixing raw text or arbitrary sentiment units into the return matrix. Add persistent book imbalance from strategy #2 as a separate experiment only if it helps.

   Relate stock-specific news to the price factors through a bounded loading-weighted feature, `factor_sentiment[k] = sum(V[i,k] * S_i) / sum(abs(V[i,k]))`, with the same fixed universe and sign convention as the price factor. Include coverage and loading concentration alongside it; one highly loaded stock's news is not automatically a basket-wide event. This projection is a feature, not a return forecast or price adjustment. Standardize the combined predictor features using their own training statistics rather than the return scaler.

   Test a small number of predeclared interactions: factor momentum with factor-aligned sentiment, broad negative news with market breadth, and negative-news intensity with rising common volatility. Positive news with positive price momentum and negative news with broad selling are confirmation hypotheses. Disagreement can reflect lag, reversal, unreliable news or a price move that already incorporated the message; it is not automatically a contrarian trade. Let out-of-sample prediction and execution results determine useful weights and gates rather than hard-coding a 50/50 blend.

   Fit a regularized linear forecast of future basket return using the combined features. Fit a separate regularized logistic model for the sharp-decline event only if there are enough independent events; assess probability calibration on chronological validation data. Keep a separately fitted and tested PCA-only fallback for feed outages. Never display a hand-weighted stress score as a calibrated probability. With sparse tail events, keep a clearly named stress alarm and report crash prediction as unvalidated. A message can describe a decline already underway: measure whether it predicts returns after actual receipt and processing, not returns before publication.

   A high first-component variance share indicates concentrated shared variation; it has no bearish sign by itself. Rising shared variation, negative factor momentum, broad declines and withdrawing liquidity together form a testable stress hypothesis. Their usefulness at this exchange and horizon must be measured. Published PCA connectedness research uses a different setting and does not establish a short-horizon Optibook edge: [Billio et al., original research](https://conference.nber.org/confer/2010/Risks10/Billio_Getmansky_Lo_Pelizzon.pdf).

   Interpretability output should show factor loadings, explained variance, current score/trend, basket breadth, relevant local feed-event IDs, event ages, sentiment/coverage, agreement or disagreement, forecast horizon, predicted return, calibrated tail probability if available, both data-source health states, and the reasons for any quote adjustment. Where the fitted model allows it, report separate price, social and interaction contributions. Distinguish “decline already occurring” from “warning issued before a subsequent decline.”

7. **Integrate the combined signal into quoting in two releases.**

   Release A uses the factor analysis and validated social stress features for risk control while retaining the existing local fair-value method. Release B adds a bounded combined forecast adjustment only after predictive and trading validation. No single message can bypass position, freshness, cost or request gates.

   | Observed/predicted condition | Proposed action |
   | --- | --- |
   | Valid data, neutral forecast | Use the validated baseline quote logic and inventory skew. |
   | Validated positive forecast | In Release B, shift the reservation price upward within a small validated bound; maintain inventory and exposure limits. |
   | Validated negative forecast | Shift it downward; avoid accumulating additional long exposure. |
   | High validated downside risk | Reduce quote size and long inventory allowance; prioritize reducing long inventory. A short position requires its own risk decision, not automatic liquidation in the wrong direction. |
   | High volatility or unstable factor basis | Widen or reduce quotes and directional exposure; do not infer a direction from variance alone. |
   | Social and price signals disagree | Apply the validated combined model; default to no extra directional tilt when disagreement is outside its tested coverage. |
   | Feed unavailable, malformed or processing delayed | Disable social features and use the separately validated PCA-only fallback; record degraded feed health. |
   | Factor warm-up or missing cross-stock data, local book valid | Disable directional overlay and allow only the approved conservative baseline/reduction policy. |
   | Local book stale, invalid, or account/order state uncertain | Withdraw affected desired quotes and stop new risk; handle existing inventory through the execution owner's recovery policy. |

   For Release B, estimate each stock's sensitivity to the basket from past returns. Map the forecast through that sensitivity and convert to price units. Raw PCA loadings on standardized returns are not directly usable dollar-price adjustments.

   ```text
   reservation_price[i] = local_fair[i]
                        + bounded_forecast_adjustment[i]
                        - inventory_penalty[i] * position[i]
   ```

   Keep the inventory target at zero initially so a quote shift and a new directional target do not double-count the same signal. Require a documented expected-edge-versus-cost/adverse-selection gate. Retain tick rounding, non-crossing prices and a threshold for changing quotes. Use persistence/hysteresis to avoid toggling around regime boundaries. Test those thresholds on validation data. Do not presume a residual from PCA mean-reverts or is an arbitrage opportunity.

   Additional portfolio controls should bound gross notional and common-factor exposure, with pending-order envelopes and confirmed contract units. They supplement the per-instrument rule; they never relax it. For every instrument and before each insertion:

   ```text
   position + outstanding_buys + pending_buys + new_buys <= 100
   position - outstanding_sells - pending_sells - new_sells >= -100
   ```

   Each outstanding or pending order must be counted exactly once, and each side independently. Apply a chosen lower internal limit as well. Never net opposite orders or other stocks to satisfy these checks. Reserve cancellations until reconciled. Portfolio allocation must reserve capacity for the whole proposed batch so instrument iteration order cannot silently over-allocate it.

8. **Preserve the strategy contract and extend evaluation explicitly.**

   Proposed implementation boundaries, all on the new branch:

   | Proposed file/module | Responsibility |
   | --- | --- |
   | `optiver_bot/features/pca_factors.py` | Pure panel validation, scaling, SVD, projection and factor diagnostics. |
   | `optiver_bot/features/social_signals.py` | Pure, versioned event parsing, relevance resolution, sentiment/novelty extraction and causal decay aggregation. |
   | `optiver_bot/features/market_regime.py` | Combine price/social features, fitted-model inference, quality gates and regime decision. |
   | `optiver_bot/strategies/pca_wall_maker.py` | Deterministic `decide(snapshot, config) -> list[DesiredOrder]`; no exchange access, training, secrets or global mutable history. |
   | `optiver_bot/pca_models.py` | Immutable typed market/social context, normalized feed events, forecast, model version and candidate configuration. |
   | Shared runner adapter | Poll social feeds centrally; maintain bounded market/event history, supply immutable context and reconciled state; own the sole connection and all order mutations. |
   | `evaluation/pca_walkforward.py` | Chronological fitting/scoring with causal feature construction and purged target boundaries. |
   | `evaluation/pca_adapter.py` | Bridge depth/history/candidate configuration to the existing replay and independent risk audit. |
   | `tests/test_pca_factors.py`, `tests/test_pca_wall_maker.py` | Mathematical/causal invariants, fallback behavior, quoting/risk integration. |
   | `tests/test_social_signals.py` | Event deduplication, causal availability, entity mapping, negation, decay, outage behavior and deterministic combined inference. |

   The shared snapshot currently has one instrument's top of book and no market history; #3 defines separate depth snapshot/config/order types. Introduce an explicit compatibility adapter and immutable factor context rather than silently replacing those types. Preserve old strategy behavior when the overlay is disabled. Be explicit that desired orders are targets, not incremental insert requests: reconciliation keeps matching orders and reserves all orders that can still fill. The adapter must prevent double-counting retained targets while never releasing unresolved exposure.

   The current replay is also insufficient without changes: its default suite uses the starter inventory maker, its input is top-of-book, and it has no history-dependent candidate configuration. Extend recording/replay to include depth needed to reproduce wall selection and social events with receive/feature-availability timestamps. Replay polling and processing delays, feed errors, duplicates and rate-budget consumption. Give all candidates the same past-only observations. Do not invent invisible depth or compare an adapted PCA strategy only with a different starter baseline and call it an improvement over #3.

   Confirm approved NumPy availability in the supplied cloud runtime; the current repository declares no dependencies. Add only approved, pinned numerical dependencies. Before client integration, read `/home/workspace/AGENTS.md` and `/home/workspace/api_reference.md`; run cloud scripts with `/opt/venv/bin/python`. Do not change credentials or supplied environment files.

9. **Prove incremental value and define promotion gates.**

   Predeclare controlled comparisons: original #3 quote logic through corrected shared execution; #4's existing pause-on-any-post behavior through that same execution layer; baseline plus simple basket momentum/volatility; baseline plus social signals only; baseline plus PCA risk controls; PCA plus bounded directional forecasts without social features; simple basket features plus social signals; and the combined PCA/social candidate. Introduce persistent imbalance only as a later experiment. Stage comparisons on development data and select one final combined candidate before opening the holdout. Use the same records, start state, costs, request limits and fill assumptions, accounting for each variant's actual polling/processing latency and request consumption. This establishes whether social feeds improve PCA, whether interpretation improves on simply pausing for news, and whether PCA adds anything beyond simple basket features plus the same news.

   Use chronological train/validation/untouched-test periods with walk-forward checks inside development data. Fit all scaling, PCA, forecasts, calibration, sentiment mappings, decay settings and thresholds without future observations. Purge observations whose forward labels cross a split boundary; use a gap at least as long as the maximum prediction horizon. Do not randomly split repeated messages or overlapping windows; deduplicate events and report dependence within news episodes. Historical lookback before a holdout may warm up features, but no holdout labels may influence selection. In an adaptive replay, refits may use only labels that have fully matured by that time, following a schedule frozen before testing. Preserve the final holdout for one frozen candidate; later research requires new future data.

   Prediction metrics: return forecast error/correlation versus zero-return and simple-momentum controls; crash precision/recall and precision-recall area versus event prevalence; false alarms per unit time; actual warning lead time; calibration/Brier score if probabilities are reported. Group overlapping crash labels into episodes when assessing evidence; many overlapping rows from one decline are not many independent crashes. Use contiguous-block uncertainty estimates and report the number of independent episodes. If evidence is too small, report inconclusive.

   Trading metrics: net PnL after confirmed costs, liquidation value with depth limitations, drawdown, post-fill markouts, turnover, fills, request counts/rejections, inventory and worst-case outstanding exposure, gross/factor exposure and unwind cost. Attribute PnL to execution edge versus carried inventory. Favorable directional exposure during one rally is not proof of improved market making.

   Stress replay with stale/missing books, factor sign flips/rotations, disappearing walls, common crashes, isolated-stock shocks, delayed inserts/cancels, partial fills and cancellation/fill races. Include duplicate/out-of-order messages, late arrival, erroneous future publication timestamps, contradictory news, unknown symbols, negation, revisions, feed outages, bursts and delayed feature processing. Test leakage by changing future prices/messages and verifying earlier forecasts/orders do not change. Verify an old published message cannot affect decisions before its actual availability. Test zero variance, insufficient history, stock reordering, SVD reconstruction, invalid costs/configuration and the independent +/-100 fill envelopes. Synthetic cases verify mechanics only. Recorded-book replay still cannot prove passive fills, queue position or real performance.

   After implementation, run `python -m unittest discover -s tests -v`, `python -m optiver_bot.demo`, the standalone #3 and #4 baseline tests, and the adapted independent risk audit. Then run the chronological replay. Passing existing tests alone is insufficient; earlier local logs show tests passing despite the reproduced cancellation/fill race. No tests were rerun for this planning-only document.

   Before looking at the final test, freeze acceptable drawdown, turnover and minimum net-benefit criteria appropriate to the actual scoring rules. Promotion requires zero risk violations, fresh-state reconciliation, acceptable request behavior, evidence of improvement over #3, #4's news pause, the simple basket/social control and the PCA-only candidate, and results that survive conservative execution/cost sensitivity. If social features add no value, retain the separately validated PCA-only candidate. If direction forecasts fail but risk controls help, retain only the validated risk controls. If tail-event evidence is insufficient, do not promote a claimed crash predictor.

   Complete an offline run, then shadow inference inside the already active execution process, then an operator-controlled small-size trial if authorized. A new dry-run connection is not an independent shadow process: #3's dry-run still connects and can displace the current bot. Keep a model-disabled fallback and rollback switch; rollback must reconcile orders and inventory rather than merely terminate the process.

The practical build order is prerequisites and synchronized market/feed recording, PCA and social-feature diagnostics, a combined risk-only overlay, causal prediction and incremental-value testing, bounded directional adjustments, then shadow/small-size evaluation. The first deliverable should make shared market risk and relevant news visible and measurable; direction-taking is conditional on evidence that it helps.
