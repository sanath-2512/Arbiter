HTML build for Chinese (language = 'zh') ships a broken search stemmer

Building HTML docs with `language = 'zh'` produces `_static/language_data.js` that ends with

    window.Stemmer = ChineseStemmer;

but the stemmer code bundled into that file is `english-stemmer.js`, which defines `EnglishStemmer`;
there is no `ChineseStemmer` anywhere. In the browser this raises a `ReferenceError` when
`language_data.js` loads, and search on the built site does not work.

The line is produced by `IndexBuilder.get_js_stemmer_code()`, which builds the class name from the
language's display name. Languages that reuse another language's stemmer file get the wrong name. The
generated code should assign the class that the bundled stemmer file actually defines (for example
`english-stemmer.js` defines `EnglishStemmer`, `dutch_porter-stemmer.js` defines `DutchPorterStemmer`).
Output for languages whose stemmer matches their name must not change.
