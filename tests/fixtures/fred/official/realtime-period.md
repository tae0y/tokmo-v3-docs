# Official FRED documentation snapshot

- Source URL: https://fred.stlouisfed.org/docs/api/fred/realtime_period.html
- Retrieved UTC: 2026-08-11T19:17:40Z
- Retrieval mode: documentation-only; no API key or provider API request

---

[Skip to main content](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html#content-container)

[Terms of Use](https://fred.stlouisfed.org/docs/api/terms_of_use.html)

# Real-Time Periods

* * *

* * *

- [Introduction](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html#introduction)
- [Examples](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html#examples)  - [Complete Real-time Period: First to Last Available](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html#example_all)
  - [Real-time Period for the 1980s Decade](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html#example_80)

## Introduction

The real-time period marks when facts were true or when information was known until it changed.
Economic data sources, releases, series, and observations are all assigned a real-time period.
Sources, releases, and series can change their names, and observation data values can be revised.

On almost all URLs, the default real-time period is today.
This can be thought of as [FRED®](https://fred.stlouisfed.org/docs/api/fred/fred.html) mode- what information about the past is available today.
[ALFRED®](https://fred.stlouisfed.org/docs/api/fred/alfred.html) users can change the real-time period to retrieve information that was known as of a past period of history.

The real-time period can be specified by setting the realtime\_start and realtime\_end variables.
Variables realtime\_start and realtime\_end are optional YYYY-MM-DD formatted dates that default to today's date.
The real-time period set by realtime\_start and realtime\_end is a (closed, closed) period.
This means that the real-time period includes the dates or boundaries set by realtime\_start and realtime\_end.

## Examples

Below are some examples for the [fred/series/observations](https://fred.stlouisfed.org/docs/api/fred/series_observations.html) endpoint.

### Complete Real-time Period: First to Last Available

The following 3 examples specified the same real-time period- the complete real-time period for which information is available:

```
https://api.stlouisfed.org/fred/series/observations?series_id=GNPCA&realtime_start=1776-07-04&realtime_end=9999-12-31&api_key=abcdefghijklmnopqrstuvwxyz123456
https://api.stlouisfed.org/fred/series/observations?series_id=GNPCA&realtime_start=1776-07-04&api_key=abcdefghijklmnopqrstuvwxyz123456
https://api.stlouisfed.org/fred/series/observations?series_id=GNPCA&realtime_end=9999-12-31&api_key=abcdefghijklmnopqrstuvwxyz123456
```

Note that API key 'abcdefghijklmnopqrstuvwxyz123456' is for demonstration purposes only. Use a [registered API key](https://fred.stlouisfed.org/docs/api/api_key.html) instead.

The first example requests all known information from the first available (1776-07-04) to the last available (9999-12-31). The second example requests all known information after the first available (1776-07-04). The third example requests all known information before the last available (9999-12-31).

### Real-time Period for the 1980s Decade

To set the real-time period for the decade of the 1980s, set realtime\_start = '1980-01-01' and realtime\_end = '1989-12-31'.

```
https://api.stlouisfed.org/fred/series/observations?series_id=GNPCA&realtime_start=1980-01-01&realtime_end=1989-12-31&api_key=abcdefghijklmnopqrstuvwxyz123456
```

To set the real-time period to 1980-01-01 and later, set realtime\_start to '1980-01-01' and leave realtime\_end unset or set realtime\_end to '9999-12-31'.

```
https://api.stlouisfed.org/fred/series/observations?series_id=GNPCA&realtime_start=1980-01-01&api_key=abcdefghijklmnopqrstuvwxyz123456
https://api.stlouisfed.org/fred/series/observations?series_id=GNPCA&realtime_start=1980-01-01&realtime_end=9999-12-31&api_key=abcdefghijklmnopqrstuvwxyz123456
```

To set the real-time period to 1980-01-01 and earlier, set realtime\_end to '1980-01-01' and leave realtime\_start unset or set realtime\_start to '1776-07-04'.

```
https://api.stlouisfed.org/fred/series/observations?series_id=GNPCA&realtime_end=1980-01-01&api_key=abcdefghijklmnopqrstuvwxyz123456
https://api.stlouisfed.org/fred/series/observations?series_id=GNPCA&realtime_start=1776-07-04&realtime_end=1980-01-01&api_key=abcdefghijklmnopqrstuvwxyz123456
```

## **Related:**

[ALFRED®](https://fred.stlouisfed.org/docs/api/fred/alfred.html), [FRED® versus ALFRED®](https://fred.stlouisfed.org/docs/api/fred/fred_vs_alfred.html)

[Back to Top](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html#top)

Filter


### Subscribe to the FRED newsletter

Subscribe

### Follow us

[Saint Louis Fed linkedin page](http://bit.ly/d056zL)[Saint Louis Fed facebook page](https://www.facebook.com/stlfed)[Saint Louis Fed X page](http://bit.ly/9ngC3L)[Saint Louis Fed YouTube page](http://bit.ly/aY9TVF)

### Need Help?

[Questions or Comments](https://fred.stlouisfed.org/contactus/)

[FRED Help](https://fredhelp.stlouisfed.org/)

* * *

[Legal](https://fred.stlouisfed.org/legal/)

[Privacy Notice & Policy](https://www.stlouisfed.org/about-us/privacy-policy/online-notice)
