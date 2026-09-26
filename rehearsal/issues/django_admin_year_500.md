Admin changelist returns a server error for very large years in the date drill-down

On a ModelAdmin with `date_hierarchy` set, crawlers hit URLs like these and get HTTP 500 instead of the
usual "invalid lookup" handling:

    /admin/events/event/?date__year=9999                     -> 500 (ValueError: year 10000 is out of range)
    /admin/events/event/?date__year=99999999999999999999     -> 500 (OverflowError)

Other bad values in the same parameters (for example `?date__month=13`) are already handled gracefully by
the changelist, which treats them as incorrect lookup parameters. Out-of-range years should be handled the
same way instead of crashing.
