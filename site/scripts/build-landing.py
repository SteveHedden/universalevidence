"""Build the public landing pages separately from the unchanged evidence app."""
from pathlib import Path
import html, re, shutil, hashlib, json
root=Path(__file__).resolve().parents[2]
out=root/'site/dist'
(out/'assets').mkdir(parents=True,exist_ok=True)
(out/'journal').mkdir(exist_ok=True)
shutil.copytree(root/'site/public/fonts',out/'fonts',dirs_exist_ok=True,copy_function=shutil.copyfile)
shutil.copy(root/'site/public/favicon.svg',out/'assets/ue-mark-transparent.svg')
shutil.copy(root/'site/public/og-landing-20261101.png',out/'og-landing-20261101.png')
shutil.copytree(root/'site/public/figures',out/'figures',dirs_exist_ok=True)
css='''@font-face{font-family:Cormorant;src:url('/fonts/cormorant-garamond-regular.ttf')}*{box-sizing:border-box}body{margin:0;background:#f6f7f9;color:#182027;font:18px/1.65 Arial,Helvetica,sans-serif}a{color:inherit;text-decoration:none}a:focus-visible,summary:focus-visible{outline:3px solid #1664a3;outline-offset:5px}header{padding:24px 5.5%;border-bottom:1px solid #d5dce3;display:flex;align-items:center;justify-content:space-between;gap:24px}.brand{display:flex;gap:14px;align-items:center;font:600 32px Cormorant,Georgia,serif;letter-spacing:.02em}.brand img{width:54px;height:54px}nav{display:flex;align-items:center;gap:36px;color:#1664a3}.button{display:inline-flex;align-items:center;gap:40px;background:#1664a3;color:white;padding:16px 24px;border-radius:3px;font-size:16px;font-weight:bold}.button:hover{background:#114e80}.arrow{font-size:24px;line-height:1}.home{padding:72px 6% 80px}.hero{max-width:1400px;margin:auto;display:grid;grid-template-columns:1.65fr 1fr;gap:7%;align-items:center;min-height:470px}h1{font:400 clamp(52px,5.6vw,92px)/1.03 Cormorant,Georgia,serif;letter-spacing:-.025em;margin:0 0 32px}h1 span{display:block;color:#1664a3}.copy p{color:#65717d;max-width:650px;margin:0 0 30px}.mark{max-width:330px;width:100%;justify-self:center;text-align:center}.mark img{width:100%;display:block}.mark figcaption{font:25px Cormorant,Georgia,serif;letter-spacing:.06em;margin-top:24px}.contribute{max-width:1400px;margin:70px auto 0;padding-top:40px;border-top:1px solid #d5dce3;display:grid;grid-template-columns:1fr 1.7fr;gap:8%}h2{font:400 38px/1.2 Cormorant,Georgia,serif;margin:0 0 22px}.contribute p{color:#65717d;margin-top:0}summary{cursor:pointer;color:#1664a3}details a,.article a{color:#1664a3;text-decoration:underline}.page{max-width:860px;margin:70px auto;padding:0 24px 80px}.page h1{font-size:64px}.page h2{margin-top:42px}.article{max-width:800px}.article h1{font-size:60px}.article p{margin:0 0 22px}.article h3{font-size:20px;margin-top:30px}.badge{color:#65717d;text-transform:uppercase;font-size:12px;letter-spacing:.15em;margin-bottom:20px}.lede{font-size:22px}.skip{position:absolute;left:-10000px}.skip:focus{left:20px;top:10px;background:white;padding:10px}@media(max-width:760px){header{padding:20px 6%;flex-wrap:wrap}.brand{font-size:27px}.brand img{width:43px;height:43px}nav{width:100%;justify-content:space-between;font-size:16px}.button{padding:13px 20px}.home{padding-top:45px}.hero{grid-template-columns:1fr;gap:44px}.mark{max-width:210px;margin:0 auto}.contribute{grid-template-columns:1fr;gap:15px;margin-top:45px}.page h1,.article h1{font-size:46px}.page{margin-top:45px}body{font-size:17px}}'''
css+='''.intervention-buckets{padding-left:24px;margin:24px 0 36px}.intervention-buckets li{padding-left:8px;margin-bottom:18px}.intervention-buckets li::marker{color:#1664a3}.intervention-buckets strong{color:#1664a3}.evidence-map{width:min(1200px,calc(100vw - 48px));position:relative;left:50%;transform:translateX(-50%);margin:48px 0}.evidence-map img{display:block;width:100%;height:auto}.evidence-map figcaption{max-width:752px;margin:16px auto 0;font-size:14px;color:#65717d}.map-scroll{overflow-x:auto}.map-scroll img{min-width:1100px}@media(max-width:760px){.evidence-map figcaption{padding:0 8px}}'''
css+='header .brand,header .brand:hover,header .brand:focus,header .brand:active{text-decoration:none}'
css+=(root/'site/content/article.css').read_text()
css+=(root/'site/content/transfer-map.css').read_text()
css+=(root/'site/content/caregiver-map.css').read_text()
css+=(root/'site/content/wash-map.css').read_text()
css+=(root/'site/content/fortification-map.css').read_text()
asset='landing-'+hashlib.sha256(css.encode()).hexdigest()[:12]+'.css'
(out/'assets'/asset).write_text(css)
header='''<a class="skip" href="#main">Skip to content</a><header><a class="brand" href="/"><img src="/assets/ue-mark-transparent.svg" alt="">Universal Evidence</a><nav aria-label="Main navigation"><a href="/about.html">About</a><a class="button" href="/explore.html">Explore the evidence <span class="arrow" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false" style="flex-shrink:0;vertical-align:-2px"><path d="M6 18 18 6M6 6h12v12"/></svg></span></a></nav></header>'''
def social_tags(title,path):
 return '<meta property="og:type" content="website"><meta property="og:site_name" content="Universal Evidence"><meta property="og:title" content="'+html.escape(title,quote=True)+'"><meta property="og:description" content="Explore research, discover who’s working on a problem, and examine the evidence for yourself."><meta property="og:url" content="https://universalevidence.com'+path+'"><meta property="og:image" content="https://universalevidence.com/og-landing-20261101.png"><meta property="og:image:type" content="image/png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630"><meta property="og:image:alt" content="Universal Evidence. Know what’s been done. Decide what’s next."><meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="'+html.escape(title,quote=True)+'"><meta name="twitter:image" content="https://universalevidence.com/og-landing-20261101.png">'
def page(title,body,path='/',draft=False):
 return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><meta name="description" content="Explore research, discover who’s working on a problem, and examine the evidence for yourself."><link rel="icon" href="/assets/ue-mark-transparent.svg"><link rel="canonical" href="https://universalevidence.com'+path+'">'+('<meta name="robots" content="noindex,nofollow">' if draft else '')+'<link rel="stylesheet" href="/assets/'+asset+'">'+social_tags(title,path)+'</head><body>'+header+body+'</body></html>'
home='''<main id="main" class="home"><section class="hero"><div class="copy"><h1>Know what’s been done.<span>Decide what’s next.</span></h1><p>Explore research, discover who’s working on a problem, and examine the evidence for yourself.</p><a class="button" href="/explore.html">Explore the evidence <span class="arrow" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false" style="flex-shrink:0;vertical-align:-2px"><path d="M6 18 18 6M6 6h12v12"/></svg></span></a></div><figure class="mark"><img src="/assets/ue-mark-transparent.svg" alt=""><figcaption>Universal Evidence</figcaption></figure></section><section class="contribute"><h2>Your work belongs here.</h2><div><p>UE discovers research and projects through public sources, including study registries and the International Aid Transparency Initiative (IATI).</p><p>Help people find your work by keeping its description, topics, locations and links up to date in those sources. Start with the record you or your organization publishes.</p><details><summary>Where to start</summary><p>For research, review your study’s registry record and links to publications and results. For development projects, review your organization’s IATI activity records, including descriptions, sectors, locations and document links.</p><p>Updates improve the information available to UE and others. Appearance in UE depends on source coverage and refresh timing.</p><p><a href="https://clinicaltrials.gov/submit-studies">ClinicalTrials.gov submission guidance <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false" style="flex-shrink:0;vertical-align:-2px"><path d="M6 18 18 6M6 6h12v12"/></svg></a><br><a href="https://iatistandard.org/en/guidance/publishing-data/">IATI publishing guidance <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false" style="flex-shrink:0;vertical-align:-2px"><path d="M6 18 18 6M6 6h12v12"/></svg></a></p></details></div></section></main>'''
(out/'index.html').write_text(re.sub(r'/assets/landing-[a-f0-9]+\.css', '/assets/'+asset, (root/'site/content/home.html').read_text()))
shutil.copy(root/'site/content/about.html',out/'about.html')
from standards_metadata import enrich as enrich_standards
(out/'open-standards.html').write_text(enrich_standards((root/'site/content/open-standards.html').read_text()))
shutil.copy(root/'site/content/contact-form.js',out/'assets/contact-form-20261030.js')
s=(root/'site/src/nutrition-draft.ts').read_text()
title=re.search(r'nutritionTitle = "(.*?)";',s).group(1)
text=re.search(r'nutritionDraft = `(.*?)`;',s,re.S).group(1)
heads={'Summary','The Problem','Solutions','Direct resource transfer','Information and behavior-change communication','Support for children and caregivers','Water and sanitation intervention','Food fortification','Conclusion'}
links=json.loads((root/'site/content/nutrition-links.json').read_text())
# Consume existing explicit source links as a unit before linking named references.
explicit_labels=['connects',"Mulago's assessment of Kidogo",'MapSan','WASH SDG','CFC financing','N3F investment','AIF','FMO']
explicit='(?:'+'|'.join(re.escape(x) for x in explicit_labels)+r') \(https://[^\s)]+\)'
named=r'(?<!\w)(?:'+'|'.join(re.escape(x) for x in sorted(links,key=len,reverse=True))+r')(?!\w)'
inline_pattern=re.compile(explicit+'|'+named)
def inline(value):
 result=[]
 position=0
 seen=set()
 for match in inline_pattern.finditer(value):
  result.append(html.escape(value[position:match.start()]))
  token=match.group()
  if ' (https://' in token:
   label,url=token.rsplit(' (',1)
   url=url[:-1]
  else:
   label,url=token,links[token]
  if label in seen and '/vocab/' in url:
   result.append(html.escape(label))
  else:
   result.append('<a href="'+html.escape(url,quote=True)+'" target="_blank" rel="noopener noreferrer">'+html.escape(label)+'</a>')
   seen.add(label)
  position=match.end()
 result.append(html.escape(value[position:]))
 return ''.join(result)
parts=[]
bucket_labels={'Direct resource transfers','Information and behavior-change communication','Support for children and caregivers','Water, sanitation and hygiene','Food fortification'}
in_buckets=False
figure='<figure class="evidence-map"><div class="map-scroll" tabindex="0" role="region" aria-label="Evidence map; scroll horizontally on smaller screens"><img src="/figures/stunting-evidence-map.svg?v=20261029-org-links" width="1440" height="1420" alt="Stunting evidence map: five intervention groups connect selected studies to examples of organizations working in related areas. Study summaries distinguish stunting from growth outcomes and identify nonrandomized evidence. Full details are described in the article below."></div><figcaption>Selected connections explored in this article. Organizations illustrate related work and are not necessarily the providers in the adjacent studies. <a href="/figures/stunting-evidence-map.svg?v=20261029-org-links" target="_blank" rel="noopener">View the full-size diagram <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false" style="flex-shrink:0;vertical-align:-2px"><path d="M6 18 18 6M6 6h12v12"/></svg></a></figcaption></figure>'
# The mobile map uses exactly the same data as the exported diagram.
import runpy
map_rows=runpy.run_path(str(root/'site/scripts/build-stunting-figure.py'))['rows']
mobile=['<div class="map-mobile"><h3>Stunting: from evidence to action</h3><p>Explore each group to see selected studies and organizations in related work.</p>']
for heading,sub,studies,orgs in map_rows:
 mobile.append('<details><summary>'+html.escape(' '.join(heading))+'</summary><div class="map-details"><p>'+html.escape(sub)+'</p><h4>Selected evidence</h4>')
 for name,lines in studies:mobile.append('<p><strong>'+html.escape(name)+'</strong><br>'+html.escape(' · '.join(lines))+'</p>')
 mobile.append('<div class="map-orgs"><h4>Organizations in related work</h4>')
 for name,description in orgs:mobile.append('<p><strong>'+inline(name)+'</strong><br>'+html.escape(description)+'</p>')
 mobile.append('</div></div></details>')
mobile.append('</div>')
figure=figure.replace('<div class="map-scroll"', ''.join(mobile)+'<div class="map-scroll"',1)
categories=['Direct resource transfer','Information and behavior-change communication','Support for children and caregivers','Water and sanitation intervention','Food fortification']
ids={name:re.sub('[^a-z0-9]+','-',name.lower()).strip('-') for name in heads}
toc=''.join('<a href="#'+ids[name]+'">'+html.escape(name)+'</a>' for name in ['Summary','The Problem']+categories+['Conclusion'])
section_open=False
action_open=False
intro_count=0
for line in text.splitlines():
 if not line.strip():continue
 label,sep,description=line.partition(': ')
 if sep and label in bucket_labels:
  if not in_buckets:parts.append('<ul class="intervention-buckets">');in_buckets=True
  parts.append('<li><strong>'+inline(label)+'</strong><br>'+inline(description)+'</li>')
  continue
 if in_buckets:parts.append('</ul>');in_buckets=False
 if line in heads:
  if action_open:parts.append('</div>');action_open=False
  if section_open:parts.append('</section>')
  if line=='The Problem':parts.append(figure)
  cls='article-section'+(' category' if line in categories else ' summary-section' if line=='Summary' else ' article-end' if line=='Conclusion' else '')
  parts.append('<section class="'+cls+'" id="'+ids[line]+'">')
  section_open=True
  number='<span class="section-number">0'+str(categories.index(line)+1)+'</span>' if line in categories else ''
  parts.append('<h2>'+number+inline(line)+'</h2>')
  if line=='Food fortification':parts.append((root/'site/content/fortification-map.html').read_text())
  if line=='Water and sanitation intervention':parts.append((root/'site/content/wash-map.html').read_text())
  if line=='Support for children and caregivers':parts.append((root/'site/content/caregiver-map.html').read_text())
  if line=='Direct resource transfer':parts.append((root/'site/content/transfer-map.html').read_text())
  if line=='Information and behavior-change communication':parts.append((root/'site/content/behavior-map.html').read_text())
 elif line=='In action':
  parts.append('<div class="in-action"><h3>In action</h3>');action_open=True
 else:
  safe=inline(line)
  cls=''
  if not section_open:
   intro_count+=1
   if intro_count==1:cls=' class="article-lead"'
  if line.startswith('Our search in Universal Evidence'):
   cls=' class="evidence-counts"'
   for phrase in ['449 studies','258 with 3,074 reported findings']:
    safe=safe.replace(phrase,'<strong>'+phrase+'</strong>')
  parts.append('<p'+cls+'>'+safe+'</p>')
  if intro_count==2 and not section_open:parts.append('<details class="mobile-contents"><summary>On this page</summary><div>'+toc+'</div></details>')
if action_open:parts.append('</div>')
if section_open:parts.append('</section>')
body='<main id="main" class="article-shell"><div class="article-hero"><div class="badge">Steve Hedden</div><h1>'+html.escape(title)+'</h1></div><div class="reading-layout"><aside class="article-toc" aria-label="On this page"><p>On this page</p>'+toc+'</aside><article class="article-content">'+''.join(parts)+'</article></div></main>'
from article_metadata import enrich
(out/'journal/childhood-stunting.html').write_text(enrich(page(title,body,'/journal/childhood-stunting.html'), title, body, links))
# Keep a permanent standards link on the generated journal page.
journal_path=out/'journal/childhood-stunting.html'
journal_doc=journal_path.read_text()
journal_path.write_text(journal_doc.replace('</body>', '<footer style="border-top:1px solid #d5dce3;padding:28px 6%;display:flex;flex-wrap:wrap;justify-content:space-between;gap:24px;font-size:14px"><span>Universal Evidence</span><span style="display:flex;flex-wrap:wrap;gap:24px"><a href="/open-standards.html">Open standards</a><a href="/about.html#contact">Contact</a></span></footer>' + '</body>'))
# Use the same analytics property as the evidence app on every public editorial page.
analytics = '<script defer src="https://cloud.umami.is/script.js" data-website-id="c1903a71-2894-474a-ab38-5d28a8a75884"></script>'
for relative in ['index.html', 'about.html', 'journal/childhood-stunting.html', 'open-standards.html']:
 document = (out/relative).read_text()
 if 'data-website-id=' not in document:
  document = document.replace('</head>', analytics + '</head>')
 assert document.count('data-website-id=') == 1, relative
 assert analytics in document, relative
 (out/relative).write_text(document)
print(out)
