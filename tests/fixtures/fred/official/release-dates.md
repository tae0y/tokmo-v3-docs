# Official FRED documentation snapshot

- Source URL: https://fred.stlouisfed.org/docs/api/fred/release_dates.html
- Retrieved UTC: 2026-08-11T19:16:30Z
- Retrieval mode: documentation-only; no API key or provider API request

---

[Skip to main content](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#content-container)

[Terms of Use](https://fred.stlouisfed.org/docs/api/terms_of_use.html)

# fred/release/dates

* * *

* * *

- [Description](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#Description)
- [Examples](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#examples)
  - [XML](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#example_xml)
  - [JSON](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#example_json)
- [Parameters](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#Parameters)  - [api\_key](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#api_key)
  - [file\_type](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#file_type)
  - [release\_id](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#release_id)
  - [realtime\_start](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#realtime_start)
  - [realtime\_end](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#realtime_end)
  - [limit](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#limit)
  - [offset](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#offset)
  - [sort\_order](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#sort_order)
  - [include\_release\_dates\_with\_no\_data](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#include_release_dates_with_no_data)

## Description

Get release dates for a release of economic data.


Note that release dates are published by data sources and do not necessarily represent when data will be available
on the [FRED](https://fred.stlouisfed.org/) or [ALFRED](https://alfred.stlouisfed.org/) websites.

## Examples

This request can return either XML or JSON by setting the [file\_type](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#file_type) parameter to xml or json.
Note that the default value of file\_type is xml.
The API key 'abcdefghijklmnopqrstuvwxyz123456' is for demonstration purposes only.
Use a [registered API key](https://fred.stlouisfed.org/docs/api/api_key.html) instead.

### XML

#### Request (HTTPS GET)

```
https://api.stlouisfed.org/fred/release/dates?release_id=82&api_key=abcdefghijklmnopqrstuvwxyz123456
```

#### Response

```xml

<release_dates realtime_start="1776-07-04" realtime_end="9999-12-31" order_by="release_date" sort_order="asc" count="17" offset="0" limit="10000">
    <release_date release_id="82">1997-02-10</release_date>
    <release_date release_id="82">1998-02-10</release_date>
    <release_date release_id="82">1999-02-04</release_date>
    <release_date release_id="82">2000-02-10</release_date>
    <release_date release_id="82">2001-01-16</release_date>
    <release_date release_id="82">2002-02-06</release_date>
    <release_date release_id="82">2003-02-07</release_date>
    <release_date release_id="82">2004-02-09</release_date>
    <release_date release_id="82">2005-02-17</release_date>
    <release_date release_id="82">2006-02-13</release_date>
    <release_date release_id="82">2007-02-16</release_date>
    <release_date release_id="82">2008-02-11</release_date>
    <release_date release_id="82">2009-03-03</release_date>
    <release_date release_id="82">2010-02-11</release_date>
    <release_date release_id="82">2011-02-23</release_date>
    <release_date release_id="82">2012-10-24</release_date>
    <release_date release_id="82">2013-04-10</release_date>
</release_dates>
```

### JSON

#### Request (HTTPS GET)

```
https://api.stlouisfed.org/fred/release/dates?release_id=82&api_key=abcdefghijklmnopqrstuvwxyz123456&file_type=json
```

#### Response

```json

{
    "realtime_start": "1776-07-04",
    "realtime_end": "9999-12-31",
    "order_by": "release_date",
    "sort_order": "asc",
    "count": 17,
    "offset": 0,
    "limit": 10000,
    "release_dates": [\
        {\
            "release_id": 82,\
            "date": "1997-02-10"\
        },\
        {\
            "release_id": 82,\
            "date": "1998-02-10"\
        },\
        {\
            "release_id": 82,\
            "date": "1999-02-04"\
        },\
        {\
            "release_id": 82,\
            "date": "2000-02-10"\
        },\
        {\
            "release_id": 82,\
            "date": "2001-01-16"\
        },\
        {\
            "release_id": 82,\
            "date": "2002-02-06"\
        },\
        {\
            "release_id": 82,\
            "date": "2003-02-07"\
        },\
        {\
            "release_id": 82,\
            "date": "2004-02-09"\
        },\
        {\
            "release_id": 82,\
            "date": "2005-02-17"\
        },\
        {\
            "release_id": 82,\
            "date": "2006-02-13"\
        },\
        {\
            "release_id": 82,\
            "date": "2007-02-16"\
        },\
        {\
            "release_id": 82,\
            "date": "2008-02-11"\
        },\
        {\
            "release_id": 82,\
            "date": "2009-03-03"\
        },\
        {\
            "release_id": 82,\
            "date": "2010-02-11"\
        },\
        {\
            "release_id": 82,\
            "date": "2011-02-23"\
        },\
        {\
            "release_id": 82,\
            "date": "2012-10-24"\
        },\
        {\
            "release_id": 82,\
            "date": "2013-04-10"\
        }\
    ]
}
```

## Parameters

### api\_key

Read [API Keys](https://fred.stlouisfed.org/docs/api/api_key.html) for more information.

- 32 character alpha-numeric lowercase string, required

### release\_id

The id for a release.

- integer, required

### file\_type

A key or file extension that indicates the type of file to send.

- string, optional, default: xml
- One of the following values: 'xml', 'json'


xml = Extensible Markup Language. The HTTP Content-Type is text/xml.


json = JavaScript Object Notation. The HTTP Content-Type is application/json.


### realtime\_start

The start of the real-time period. For more information, see [Real-Time Periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html).

- YYYY-MM-DD formatted string, optional, default: 1776-07-04 (earliest available)

### realtime\_end

The end of the real-time period. For more information, see [Real-Time Periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html).

- YYYY-MM-DD formatted string, optional, default: 9999-12-31 (latest available)

### limit

The maximum number of results to return.

- integer between 1 and 10000, optional, default: 10000

### offset

- non-negative integer, optional, default: 0

### sort\_order

Sort results is ascending or descending release date order.

- One of the following strings: 'asc', 'desc'.
- optional, default: asc

### include\_release\_dates\_with\_no\_data

Determines whether release dates with no data available are returned.
The defalut value 'false' excludes release dates that do not have data.
In particular, this excludes future release dates which may be available in the
[FRED release calendar](https://fred.stlouisfed.org/releases/calendar) or the [ALFRED release calendar](https://alfred.stlouisfed.org/releases/calendar).

- One of the following strings: 'true', 'false'.
- optional, default: false

[Back to Top](https://fred.stlouisfed.org/docs/api/fred/release_dates.html#top)

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
