ETag is no longer generated when a Transfer-Encoding header is set

Since the recent change that stops res.send() from adding Content-Length when a Transfer-Encoding header is
present, responses that set Transfer-Encoding no longer get an ETag either:

```js
app.get('/', function (req, res) {
  res.set('Transfer-Encoding', 'chunked')
  res.send('hello, world')
})
// response: no ETag header (it had one before), so conditional GETs never get a 304
```

Content-Length must still be left out when Transfer-Encoding is present, but ETag generation should work as
it does for any other response.
