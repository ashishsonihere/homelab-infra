-- Map the researched source catalog into the `sources` table so it's queryable in Postgres/pgAdmin.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS track  text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS tier   int;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS access text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS signal text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS status text DEFAULT 'planned';

INSERT INTO sources (slug, name, kind, base_url, track, tier, access, signal, status) VALUES
-- Track A — software (LIVE)
('hackernews','Hacker News','community','https://news.ycombinator.com','A',1,'api','founder/dev pain, launches','live'),
('news_rss','Tech news (RSS)','news','https://techcrunch.com','A',1,'feed','startup/funding news','live'),
('ycombinator','YC companies','funding','https://ycombinator.com/companies','A',1,'api','funded categories/patterns','live'),
('stackexchange','Stack Exchange','community','https://stackexchange.com','A',1,'api','tool-recommendation demand','live'),
-- Track A — software (planned)
('reddit','Reddit','community','https://reddit.com','A',1,'api(key)','pain points, workarounds','needs-key'),
('producthunt','Product Hunt','launch','https://producthunt.com','A',1,'api(key)','hot launches/traction','needs-key'),
('shopify_app_store','Shopify App Store','marketplace','https://apps.shopify.com','A',1,'scrape(proxy)','new-app+review demand','needs-proxy'),
('alternativeto','AlternativeTo','review','https://alternativeto.net','A',1,'scrape(proxy)','leaving X for Y','planned'),
('g2','G2','review','https://g2.com','A',1,'scrape(proxy)','competitor weakness','planned'),
('capterra','Capterra','review','https://capterra.com','A',1,'scrape(proxy)','SMB demand/categories','planned'),
('getapp','GetApp','review','https://getapp.com','A',2,'scrape(proxy)','category map','planned'),
('trustradius','TrustRadius','review','https://trustradius.com','A',2,'scrape(proxy)','enterprise pain','planned'),
('gartner_peerinsights','Gartner Peer Insights','review','https://gartner.com/reviews','A',2,'scrape(proxy)','enterprise weakness','planned'),
('peerspot','PeerSpot','review','https://peerspot.com','A',2,'scrape(proxy)','IT/dev pain','planned'),
('saashub','SaaSHub','review','https://saashub.com','A',2,'scrape(proxy)','alternatives/category','planned'),
('indiehackers','Indie Hackers','community','https://indiehackers.com','A',1,'scrape','indie traction+struggles','planned'),
('betalist','BetaList','launch','https://betalist.com','A',2,'scrape','pre-launch demand','planned'),
('failory','Failory','funding','https://failory.com','A',1,'scrape','why startups failed','planned'),
('crunchbase','Crunchbase','funding','https://crunchbase.com','A',2,'api(paid)','funding by category','paid'),
('dealroom','Dealroom (EU)','funding','https://dealroom.co','A',2,'api(paid)','EU funding','paid'),
('wellfound','Wellfound','funding','https://wellfound.com','A',2,'scrape','what is being built/hiring','planned'),
('sec_edgar','SEC EDGAR','funding','https://sec.gov/edgar','A',2,'api','filings (US)','planned'),
('opencorporates','OpenCorporates','funding','https://opencorporates.com','A',2,'api','company registry','planned'),
('taaft','There''s An AI For That','launch','https://theresanaiforthat.com','A',1,'scrape','AI niche crowding','planned'),
('appsumo','AppSumo','launch','https://appsumo.com','A',2,'scrape','what SMBs buy','planned'),
('exploding_topics','Exploding Topics','trend','https://explodingtopics.com','A',1,'scrape','breakout terms','planned'),
('sifted','Sifted (EU)','news','https://sifted.eu','A',2,'feed','EU startup news','planned'),
-- Track B — physical / trade (planned; free-key or proxy)
('un_comtrade','UN Comtrade','trade','https://comtradeplus.un.org','B',1,'api(key)','global category growth','needs-key'),
('us_census_trade','US Census Intl Trade','trade','https://api.census.gov','B',1,'api(key)','US import/export by HS','needs-key'),
('usitc_dataweb','USITC DataWeb','trade','https://dataweb.usitc.gov','B',1,'api(key)','US trade+tariffs','needs-key'),
('eurostat_comext','Eurostat Comext','trade','https://ec.europa.eu/eurostat','B',1,'api','EU trade by CN8','planned'),
('uk_hmrc','UK HMRC trade','trade','https://uktradeinfo.com','B',1,'api','UK trade','planned'),
('oec','OEC (oec.world)','trade','https://oec.world','B',1,'api','product space/growth','planned'),
('world_bank_wits','World Bank WITS','trade','https://wits.worldbank.org','B',1,'api','trade+tariffs','planned'),
('importyeti','ImportYeti','trade','https://importyeti.com','B',1,'scrape(proxy)','US customs/suppliers','planned'),
('volza','Volza','trade','https://volza.com','B',2,'paid','global shipments','paid'),
('indiamart','IndiaMART','b2b','https://indiamart.com','B',1,'scrape(proxy)','India demand/supply/pricing','planned'),
('alibaba','Alibaba/1688','b2b','https://alibaba.com','B',1,'scrape(proxy)','global trend/sourcing','planned'),
('amazon_bestsellers','Amazon Best Sellers/Movers','marketplace','https://amazon.com/gp/bestsellers','B',1,'scrape(proxy)','what is surging','needs-proxy'),
('sellersprite','SellerSprite','tool','https://sellersprite.com','B',1,'export(paid)','demand/competition','paid'),
('keepa','Keepa','tool','https://keepa.com','B',2,'api(paid)','price/BSR history','paid'),
('statista','Statista','market','https://statista.com','B',1,'scrape','market size/CAGR','planned'),
('grand_view','Grand View Research','market','https://grandviewresearch.com','B',1,'scrape','category CAGR','planned'),
('google_trends','Google Trends','trend','https://trends.google.com','B',1,'scrape','rising interest','planned'),
('glimpse','Glimpse','trend','https://meetglimpse.com','B',1,'scrape(paid)','durable vs fad','planned'),
('charm_io','Charm.io','d2c','https://charm.io','B',2,'paid','rising D2C brands','paid'),
('inc42','Inc42 (India D2C)','d2c','https://inc42.com','B',1,'feed','India D2C categories','planned')
ON CONFLICT (slug) DO UPDATE SET
  name=EXCLUDED.name, kind=EXCLUDED.kind, base_url=EXCLUDED.base_url,
  track=EXCLUDED.track, tier=EXCLUDED.tier, access=EXCLUDED.access,
  signal=EXCLUDED.signal, status=EXCLUDED.status;
