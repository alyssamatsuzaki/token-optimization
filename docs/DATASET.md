# The demo dataset

Generated from `data/demo/policy.yaml` by `tokop dataset --write`. Every gold answer is
**computed from the policy in code**, never written by hand, so a question and its answer
cannot drift apart. `tokop fixtures-check` regenerates the whole set and fails the
build if the committed file no longer matches.

Read this before running `make record`: once you set `RECORD_BUDGET_USD`, these are the
questions your money will be spent answering.

## Shape

- 300 questions, 100 calibration and 200 test, stratified by question type with seed 20260911.
- Handbook: 21,768 characters, 6,064 tokens on `bytes-bpe-approx-v1`.
- The router never sees the question type or the supporting section IDs. They are recorded for analysis after the fact only.

| Question type | Count | Share |
| --- | ---: | ---: |
| computation | 60 | 20% |
| exception | 30 | 10% |
| lookup | 135 | 45% |
| two_hop | 75 | 25% |

## Samples

### computation

| Question | Gold | Supporting quote |
| --- | --- | --- |
| Work out the shipping charged on a $68 order, 6.5 lb, to Zone 1 (in-state), for an Alpine member. | `0.00` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |
| Calculate shipping for a 2 lb, $32 order to Zone 5 (international) placed by a Summit member. | `32.95` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |
| How much shipping does a Summit customer pay on $68 weighing 28 lb sent to Zone 3 (continental)? | `0.00` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |
| Work out the shipping charged on a $110 order, 55 lb, to Zone 3 (continental), for a Summit member. | `0.00` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |
| How much shipping does a Standard customer pay on $32 weighing 2 lb sent to Zone 1 (in-state)? | `5.95` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |
| Work out the shipping charged on a $32 order, 14 lb, to Zone 1 (in-state), for a Summit member. | `17.95` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |
| Calculate shipping for a 55 lb, $110 order to Zone 4 (Alaska and Hawaii) placed by an Alpine member. | `0.00` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |
| Calculate shipping for a 2 lb, $32 order to Zone 4 (Alaska and Hawaii) placed by an Alpine member. | `0.00` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |
| Calculate shipping for a 14 lb, $32 order to Zone 5 (international) placed by an Alpine member. | `0.00` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |
| Standard tier, $110 order, 2 lb, Zone 3 (continental). Shipping in dollars? | `0.00` | ## Shipping fees Fees are charged per order, by the total shipped weight, not per item. Weight bands are inclusive of their upper bound: a parcel weig… |

### exception

| Question | Gold | Supporting quote |
| --- | --- | --- |
| A final-sale item arrived faulty. How many days does the customer have to report it? | `7` | ## Exceptions An exception overrides the general rule for the categories it names. Where two exceptions could apply, the more restrictive one wins. **… |
| How many days after delivery can a defect on a final-sale item still be raised? | `7` | ## Exceptions An exception overrides the general rule for the categories it names. Where two exceptions could apply, the more restrictive one wins. **… |
| Defect reporting window for final-sale goods, in days? | `7` | ## Exceptions An exception overrides the general rule for the categories it names. Where two exceptions could apply, the more restrictive one wins. **… |
| A Standard member bought clearance apparel. How many days is their return window? | `0` | ## Return windows by category The base return window runs from the delivery date, not the order date. A return is counted as started on the day the cu… |
| An Alpine member bought trail nutrition. How many days is their return window? | `0` | ## Return windows by category The base return window runs from the delivery date, not the order date. A return is counted as started on the day the cu… |
| Alpine tier, clearance apparel. Return window in days? | `0` | ## Return windows by category The base return window runs from the delivery date, not the order date. A return is counted as started on the day the cu… |
| Standard tier, trail nutrition. Return window in days? | `0` | ## Return windows by category The base return window runs from the delivery date, not the order date. A return is counted as started on the day the cu… |
| Return window in days for trail nutrition bought by a Summit member? | `0` | ## Return windows by category The base return window runs from the delivery date, not the order date. A return is counted as started on the day the cu… |
| Seal opened, navigation electronics, Summit tier. Restocking fee percentage? | `25` | ## Exceptions An exception overrides the general rule for the categories it names. Where two exceptions could apply, the more restrictive one wins. **… |
| Summit customer, broken seal on a GPS unit. Restocking percentage? | `25` | ## Exceptions An exception overrides the general rule for the categories it names. Where two exceptions could apply, the more restrictive one wins. **… |

### lookup

| Question | Gold | Supporting quote |
| --- | --- | --- |
| Annual fee for Alpine, in dollars? | `129.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |
| What does a year of Alpine membership cost? | `129.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |
| Annual fee for Summit, in dollars? | `49.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |
| A customer asks what Summit costs annually. How much? | `49.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |
| What does a year of Summit membership cost? | `49.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |
| What is the annual fee for the Alpine tier? | `129.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |
| What does a year of Standard membership cost? | `0.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |
| How much does Summit membership cost per year, in dollars? | `49.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |
| Annual fee for Standard, in dollars? | `0.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |
| A customer asks what Alpine costs annually. How much? | `129.00` | ## Loyalty tiers Every customer sits in one of three tiers. The tier changes three things: how long they have to return something, how much they must … |

### two_hop

| Question | Gold | Supporting quote |
| --- | --- | --- |
| For a refund of $501 with no safety concern, name the escalation level key that handles it. | `tier_3` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |
| For a refund of $850 with no safety concern, name the escalation level key that handles it. | `tier_3` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |
| For a refund of $75 with no safety concern, name the escalation level key that handles it. | `tier_1` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |
| Route a $480 refund request with no injury or safety failure. Which level key takes it? | `tier_1` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |
| $480 refund, routine case. Which escalation level key applies? | `tier_1` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |
| Who handles a $1200 refund with nothing safety-related about it? Give the escalation level key. | `tier_3` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |
| Route a $75 refund request with no injury or safety failure. Which level key takes it? | `tier_1` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |
| Route a $850 refund request with no injury or safety failure. Which level key takes it? | `tier_3` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |
| A refund of $850 is requested with no safety issue. Which escalation level handles it? Answer with the level key. | `tier_3` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |
| A refund of $480 is requested with no safety issue. Which escalation level handles it? Answer with the level key. | `tier_1` | ## Escalation | Level | Key | Handles | Resolution target | | --- | --- | --- | --- | | Front-line support | `tier_1` | standard returns, shipping que… |

## How each answer is computed

Every template names the policy function that produces its gold answer. There is no second implementation to disagree with.

| Template | Type | Answer | Sections |
| --- | --- | --- | --- |
| `cp_order_shipping` | computation | money | sec-shipping-fees, sec-free-shipping |
| `cp_oversize_shipping` | computation | money | sec-shipping-fees |
| `cp_refund_amount` | computation | money | sec-restocking, sec-loyalty |
| `cp_restocking_amount` | computation | money | sec-restocking, sec-loyalty |
| `cp_seasonal_window` | computation | number | sec-returns-windows, sec-loyalty, sec-seasonal |
| `ex_final_sale_defect_days` | exception | number | sec-exceptions |
| `ex_final_sale_window` | exception | number | sec-returns-windows, sec-loyalty |
| `ex_opened_electronics_fee` | exception | number | sec-exceptions, sec-loyalty |
| `ex_opened_electronics_window` | exception | number | sec-exceptions |
| `ex_safety_escalation` | exception | enum | sec-escalation |
| `ex_used_safety_gear` | exception | yes_no | sec-exceptions |
| `ex_worn_footwear_refund` | exception | string | sec-exceptions |
| `lu_annual_fee` | lookup | money | sec-loyalty |
| `lu_band_fee` | lookup | money | sec-shipping-fees |
| `lu_escalation_hours` | lookup | number | sec-escalation |
| `lu_final_sale` | lookup | yes_no | sec-returns-windows |
| `lu_free_ship_threshold` | lookup | money | sec-free-shipping |
| `lu_packaging` | lookup | yes_no | sec-packaging |
| `lu_restocking_pct` | lookup | number | sec-restocking |
| `lu_return_window` | lookup | number | sec-returns-windows |
| `lu_tier_bonus` | lookup | number | sec-loyalty |
| `lu_transit_days` | lookup | number | sec-shipping-zones |
| `lu_warranty_years` | lookup | number | sec-warranty |
| `th_escalation_level` | two_hop | enum | sec-escalation |
| `th_fee_with_tier` | two_hop | number | sec-restocking, sec-loyalty |
| `th_free_shipping` | two_hop | yes_no | sec-free-shipping, sec-loyalty |
| `th_window_with_tier` | two_hop | number | sec-returns-windows, sec-loyalty |
| `th_zone_weight_fee` | two_hop | money | sec-shipping-fees |
