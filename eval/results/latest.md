# Baseline vs Hindsight Copilot (2026-09-28T06:29:52Z)

Model: `openai/gpt-oss-120b` (baseline `openai/gpt-oss-120b`) · memory: hindsight

| Metric | Baseline | Copilot |
|---|---|---|
| Right root cause | 3/8 | 8/8 |
| Right first fix (all) | 1/8 | 8/8 |
| Right first fix (field-pattern faults) | 0/6 | 6/6 |
| Right first fix (control faults, manual correct) | 1/2 | 2/2 |
| Safety step present | 8/8 | 8/8 |
| Citations all verified | n/a | 7/8 |

Parts spend avoided where the baseline would have swapped a part first: **$7,455** and ~21.0 labor hours.

| Scenario | Baseline first fix | Copilot first fix |
|---|---|---|
| e412-chl0417 | ❌ **Supply check** - measure line-to-line voltage on all three phases; c | ✅ Visually inspect the J4 power harness for looseness or chafing. |
| gf17-inv3140-desert | ❌ **Inspect & remediate the offending string** (identified as string 7): | ✅ **Document** - Record current telemetry values (Riso, leakage, string- |
| gf17-inv2267-coastal | ❌ **Dry & protect** - If moisture is present, gently dry with lint-free  | ✅ **Clean & replace grounding strap** |
| f082-elv0215 | ❌ **Visual inspection** - Confirm encoder mounting bolts are torqued to  | ✅ **Inspect encoder cable** - confirm the shield is continuous and bonde |
| e221-chl0522 | ❌ **Verify sensor deviation** | ✅ **Clean the thermowell interior** - scrape any old compound, wipe with |
| ot05-inv2231 | ❌ **Run the built-in fan diagnostic** (Service → Diagnostics → Fan Test) | ✅ **Run NTC calibration** - In the inverter diagnostics, select **NTC Ca |
| f119-elv0109-control | ❌ **Access the brake assembly** - Remove the protective cover on the bra | ✅ **Quick field check - air gap** |
| com31-inv3107-control | ✅ **Inspect termination** - Locate the RS-485 bus termination at the far | ✅ **Verify termination** - With the inverter powered down, measure the r |
