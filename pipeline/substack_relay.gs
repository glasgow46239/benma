/**
 * Substack relay for site/articles.json.
 * Substack blocks GitHub's servers, so the nightly run asks Google to fetch the feed instead.
 *
 * Set-up, once:
 *  1. Go to script.google.com > New project. Paste this whole file over the starter code. Save.
 *  2. Deploy > New deployment > type "Web app". Execute as: Me. Who has access: Anyone. Deploy.
 *  3. Authorise when asked, then copy the Web app URL (it ends /exec).
 *  4. Paste it into RELAY near the top of pipeline/articles.py and commit.
 * Open the URL in a browser to check it: you should see JSON with your latest posts.
 */
var FEED = 'https://britainelects.substack.com/feed';

function doGet() {
  var res = UrlFetchApp.fetch(FEED, { muteHttpExceptions: true, followRedirects: true });
  var out = { status: res.getResponseCode(), items: [] };
  if (out.status === 200) {
    var channel = XmlService.parse(res.getContentText()).getRootElement().getChild('channel');
    var content = XmlService.getNamespace('content', 'http://purl.org/rss/1.0/modules/content/');
    out.items = channel.getChildren('item').slice(0, 30).map(function (it) {
      var enc = it.getChild('enclosure'), image = enc && enc.getAttribute('url') ? enc.getAttribute('url').getValue() : '';
      if (!image) {
        var m = /<img[^>]+src="([^"]+)"/.exec(it.getChildText('encoded', content) || '');
        image = m ? m[1] : '';
      }
      var pub = it.getChildText('pubDate');
      return {
        title: it.getChildText('title') || '',
        url: it.getChildText('link') || '',
        date: pub ? Utilities.formatDate(new Date(pub), 'Europe/London', 'yyyy-MM-dd') : '',
        desc: (it.getChildText('description') || '').replace(/<[^>]+>/g, '').trim(),
        image: image
      };
    });
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}
