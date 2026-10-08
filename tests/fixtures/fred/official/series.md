# Official FRED documentation snapshot

- Source URL: https://fred.stlouisfed.org/docs/api/fred/series.html
- Retrieved UTC: 2026-08-11T19:16:30Z
- Retrieval mode: documentation-only; no API key or provider API request

---

[Skip to main content](https://fred.stlouisfed.org/docs/api/fred/series.html#content-container)

[Terms of Use](https://fred.stlouisfed.org/docs/api/terms_of_use.html)

# fred/series

* * *

* * *

- [Description](https://fred.stlouisfed.org/docs/api/fred/series.html#Description)
- [Examples](https://fred.stlouisfed.org/docs/api/fred/series.html#examples)
  - [XML](https://fred.stlouisfed.org/docs/api/fred/series.html#example_xml)
  - [JSON](https://fred.stlouisfed.org/docs/api/fred/series.html#example_json)
- [Parameters](https://fred.stlouisfed.org/docs/api/fred/series.html#Parameters)  - [api\_key](https://fred.stlouisfed.org/docs/api/fred/series.html#api_key)
  - [file\_type](https://fred.stlouisfed.org/docs/api/fred/series.html#file_type)
  - [series\_id](https://fred.stlouisfed.org/docs/api/fred/series.html#series_id)
  - [realtime\_start](https://fred.stlouisfed.org/docs/api/fred/series.html#realtime_start)
  - [realtime\_end](https://fred.stlouisfed.org/docs/api/fred/series.html#realtime_end)

## Description

Get an economic data series.

## Examples

This request can return either XML or JSON by setting the [file\_type](https://fred.stlouisfed.org/docs/api/fred/series.html#file_type) parameter to xml or json.
Note that the default value of file\_type is xml.
The API key 'abcdefghijklmnopqrstuvwxyz123456' is for demonstration purposes only.
Use a [registered API key](https://fred.stlouisfed.org/docs/api/api_key.html) instead.

### XML

#### Request (HTTPS GET)

```
https://api.stlouisfed.org/fred/series?series_id=GNPCA&api_key=abcdefghijklmnopqrstuvwxyz123456
```

#### Response

```xml

<seriess realtime_start="2013-08-14" realtime_end="2013-08-14">
    <series id="GNPCA" realtime_start="2013-08-14" realtime_end="2013-08-14" title="Real Gross National Product" observation_start="1929-01-01" observation_end="2012-01-01" frequency="Annual" frequency_short="A" units="Billions of Chained 2009 Dollars" units_short="Bil. of Chn. 2009 $" seasonal_adjustment="Not Seasonally Adjusted" seasonal_adjustment_short="NSA" last_updated="2013-07-31 09:26:16-05" popularity="39" notes="BEA Account Code: A001RX1"/>
</seriess>
```

The series tag's notes attribute is optional.

### JSON

#### Request (HTTPS GET)

```
https://api.stlouisfed.org/fred/series?series_id=GNPCA&api_key=abcdefghijklmnopqrstuvwxyz123456&file_type=json
```

#### Response

```json

{
    "realtime_start": "2013-08-14",
    "realtime_end": "2013-08-14",
    "seriess": [\
        {\
            "id": "GNPCA",\
            "realtime_start": "2013-08-14",\
            "realtime_end": "2013-08-14",\
            "title": "Real Gross National Product",\
            "observation_start": "1929-01-01",\
            "observation_end": "2012-01-01",\
            "frequency": "Annual",\
            "frequency_short": "A",\
            "units": "Billions of Chained 2009 Dollars",\
            "units_short": "Bil. of Chn. 2009 $",\
            "seasonal_adjustment": "Not Seasonally Adjusted",\
            "seasonal_adjustment_short": "NSA",\
            "last_updated": "2013-07-31 09:26:16-05",\
            "popularity": 39,\
            "notes": "BEA Account Code: A001RX1"\
        }\
    ]
```

## Parameters

### api\_key

Read [API Keys](https://fred.stlouisfed.org/docs/api/api_key.html) for more information.

- 32 character alpha-numeric lowercase string, required

### file\_type

A key or file extension that indicates the type of file to send.

- string, optional, default: xml
- One of the following values: 'xml', 'json'


xml = Extensible Markup Language. The HTTP Content-Type is text/xml.


json = JavaScript Object Notation. The HTTP Content-Type is application/json.


### series\_id

The id for a series.

- string, required

### realtime\_start

The start of the real-time period. For more information, see [Real-Time Periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html).

- YYYY-MM-DD formatted string, optional, default: today's date

### realtime\_end

The end of the real-time period. For more information, see [Real-Time Periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html).

- YYYY-MM-DD formatted string, optional, default: today's date

[Back to Top](https://fred.stlouisfed.org/docs/api/fred/series.html#top)

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
