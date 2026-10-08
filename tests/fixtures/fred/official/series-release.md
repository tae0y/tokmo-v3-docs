# Official FRED documentation snapshot

- Source URL: https://fred.stlouisfed.org/docs/api/fred/series_release.html
- Retrieved UTC: 2026-08-11T19:16:30Z
- Retrieval mode: documentation-only; no API key or provider API request

---

[Skip to main content](https://fred.stlouisfed.org/docs/api/fred/series_release.html#content-container)

[Terms of Use](https://fred.stlouisfed.org/docs/api/terms_of_use.html)

# fred/series/release

* * *

* * *

- [Description](https://fred.stlouisfed.org/docs/api/fred/series_release.html#Description)
- [Examples](https://fred.stlouisfed.org/docs/api/fred/series_release.html#examples)
  - [XML](https://fred.stlouisfed.org/docs/api/fred/series_release.html#example_xml)
  - [JSON](https://fred.stlouisfed.org/docs/api/fred/series_release.html#example_json)
- [Parameters](https://fred.stlouisfed.org/docs/api/fred/series_release.html#Parameters)  - [api\_key](https://fred.stlouisfed.org/docs/api/fred/series_release.html#api_key)
  - [file\_type](https://fred.stlouisfed.org/docs/api/fred/series_release.html#file_type)
  - [series\_id](https://fred.stlouisfed.org/docs/api/fred/series_release.html#series_id)
  - [realtime\_start](https://fred.stlouisfed.org/docs/api/fred/series_release.html#realtime_start)
  - [realtime\_end](https://fred.stlouisfed.org/docs/api/fred/series_release.html#realtime_end)

## Description

Get the release for an economic data series.

## Examples

This request can return either XML or JSON by setting the [file\_type](https://fred.stlouisfed.org/docs/api/fred/series_release.html#file_type) parameter to xml or json.
Note that the default value of file\_type is xml.
The API key 'abcdefghijklmnopqrstuvwxyz123456' is for demonstration purposes only.
Use a [registered API key](https://fred.stlouisfed.org/docs/api/api_key.html) instead.

### XML

#### Request (HTTPS GET)

```
https://api.stlouisfed.org/fred/series/release?series_id=IRA&api_key=abcdefghijklmnopqrstuvwxyz123456
```

#### Response

```xml

<releases realtime_start="2013-08-13" realtime_end="2013-08-13">
    <release id="21" realtime_start="2013-08-13" realtime_end="2013-08-13" name="H.6 Money Stock Measures" press_release="true" link="http://www.federalreserve.gov/releases/h6/"/>
</releases>
```

The release tag's link and notes attributes are optional.

### JSON

#### Request (HTTPS GET)

```
https://api.stlouisfed.org/fred/series/release?series_id=IRA&api_key=abcdefghijklmnopqrstuvwxyz123456&file_type=json
```

#### Response

```json

{
    "realtime_start": "2013-08-14",
    "realtime_end": "2013-08-14",
    "releases": [\
        {\
            "id": 21,\
            "realtime_start": "2013-08-14",\
            "realtime_end": "2013-08-14",\
            "name": "H.6 Money Stock Measures",\
            "press_release": true,\
            "link": "http://www.federalreserve.gov/releases/h6/"\
        }\
    ]
}
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

[Back to Top](https://fred.stlouisfed.org/docs/api/fred/series_release.html#top)

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
