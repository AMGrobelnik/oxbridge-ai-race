# Winter epidemic policy handbook

You are choosing one policy for a 10M-person region over a 180-day winter wave of a respiratory
virus. The simulator is an age-structured SEIRV model with five bands (0-17, 18-39, 40-59, 60-74,
75+), POLYMOD-style contacts split into home, school, work and other settings, and R0 near 2
(it varies by seed). About 25-30% of people start immune. Every run is stochastic: seeding day,
seed size, R0, vaccine uptake and imported cases vary by seed. Simulation evidence, not policy advice.

## What the score rewards

Score per seed, in thousands (higher is better):

    (hospital-days averted vs no intervention - penalties) / total cost in GBP m

- **Averted hospital-days** compare your winter with the same winter and no policy at all.
- **ICU overflow:** every ICU patient-day without a staffed bed costs 100 hospital-day
  equivalents (overflow patients die at about 5% a day).
- **School closures:** every school-closure day beyond 21 costs 5,000 hospital-day equivalents.
- **Age gap:** if the 60+ share of hospital-days rises compared with no intervention, each
  0.01 rise costs 10,000 hospital-day equivalents.
- **Total cost** is your planned spend plus GBP 0.4M of furlough top-up per workplace-closure day.
  Planned spend must stay within **GBP 40M**.

## Levers (the policy JSON) and their prices

| Key | Range | Effect | Cost |
|---|---|---|---|
| `vaccine_share.<band>` | 0-1 each, sum <= 1 | Share of a 3M single-dose supply per band | GBP 4 per dose bought (GBP 12M if the shares sum to 1) |
| `vaccine_start_day` | 0-120 (integer) | Campaign start; 60k doses a day from then | GBP 0.2M per day earlier than day 45 |
| `school_close_trigger_per_100k` | 5-500 | Close schools at this hospital occupancy per 100k; reopen below 70% of it | none, but see the 21-day cap |
| `school_close_max_days` | 0-60 (integer) | Total closure days allowed | none |
| `work_close_trigger_per_100k` | 5-500 | Close workplaces at this occupancy; reopen below 70% | GBP 0.4M per closed day (counts in cost) |
| `test_isolate_level` | 0-1 | Cuts transmission by up to 35%; the effect halves at about 150 new cases per 100k a day | GBP 30M x level |
| `surge_icu_beds` | 0-1000 (integer) | ICU beds staffed on top of 300 | GBP 25k per bed |

## Domain notes

- **Vaccines:** the vaccine is leaky. It cuts infection risk by about half and cuts admission
  and death risk for vaccinated cases by a further 70%. Hospitalisation per infection rises
  steeply with age (0.2% at 0-17 to 15% at 75+); ICU need peaks in the 40-74 bands. Doses
  given to a band beyond the share of people willing to take them are wasted (willingness
  ranges from about 50% of children to 90% of over-75s, with noise).
- **Timing:** the wave is usually seeded in the second week. Earlier vaccination protects more
  people before the peak but costs an acceleration premium.
- **Contacts:** children have the most contacts, mostly at school; working-age adults mix at
  work. Closing schools or workplaces cuts transmission but closures over the cap or long work
  closures are expensive in the score.
- **ICU capacity:** 300 beds are free for the epidemic. The peak ICU load is the quantity most
  sensitive to how transmissible the virus turns out to be; surge beds cost money every winter
  whether or not they are used.
- **Test-and-isolate** is most cost-effective while incidence is low and saturates in a big wave.
- **Trade-off:** the score is a ratio, so cheaper policies look efficient, but a winter is
  uncertain; a policy is judged on more than the scenario you can see.

## Output

Return the full policy object with every key, numbers only, no extra keys.
