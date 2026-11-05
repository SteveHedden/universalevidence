from pathlib import Path
from html import escape
import json
root=Path(__file__).resolve().parents[2]
links=json.loads((root/'site/content/nutrition-links.json').read_text())
rows=[
(['Direct resource','transfers'], 'Supplements · cash · vouchers', [('SHINE · Zimbabwe · 2019',['Supplements + feeding counselling','21% lower stunting prevalence at 18 months']),('REFANI · Pakistan · 2017',['Cash or vouchers added to services','Lower odds of stunting'])], [('Action Against Hunger','Cash and voucher programmes'),('World Food Programme','Food, cash and vouchers')]),
(['Information &','behaviour change'], 'Education · practical feeding support', [('Kang · Ethiopia · 2017',['Community nutrition programme','8.1 percentage points lower stunting at 1 year']),('Shonchoy · Pakistan · 2023',['Growth monitoring + counselling','10 percentage points lower stunting; nonrandomized'])], [('World Vision','Community nutrition programmes'),('Mediae','Cooking and nutrition edutainment')]),
(['Children &','caregiver support'], 'Childcare · home visits · mental health', [('Khan · Pakistan · 2018',['Integrated care and caregiver support','21 percentage points lower stunting at 12 months']),('Gope · India · 2019',['Day care, meals and family support','27% lower stunting odds; nonrandomized'])], [('Kidogo','Childcare with nutrition support')]),
(['Water &','sanitation'], 'Water treatment · sanitation · hygiene', [('CHoBI7 · Bangladesh · 2021',['WASH supplies, education + messages','Lower odds of stunting']),('MapSan · Mozambique · 2025',['Shared toilets; better growth, lower stunting odds','Nonrandomized follow-up · preprint'])], [('WSUP','Improved shared sanitation'),('Sanivation','Sanitation and waste treatment')]),
(['Food','fortification'], 'Micronutrient-fortified foods', [('Sazawal · India · 2010',['Fortified milk; height-for-age z-score','+0.19 greater improvement']),('Sazawal · Bangladesh · 2013',['Fortified yogurt; height-for-age z-score','0.06 higher than comparison'])], [('Shalem Investment','Fortified flour'),('Africa Improved Foods','Fortified cereals and porridges')])]
# Keep the map self-contained so exported SVGs retain UE typography.
import base64, textwrap
font=base64.b64encode((root/'site/public/fonts/cormorant-garamond-regular.ttf').read_bytes()).decode()
svg=['<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="1420" viewBox="0 0 1440 1420" role="img" aria-labelledby="title desc">',
 '<title id="title">Stunting: from evidence to action</title><desc id="desc">Five intervention groups connect selected stunting and growth studies with examples of organizations working in related areas. Organizations are not necessarily the providers in adjacent studies. Findings are not pooled estimates.</desc>',
 '<rect width="1440" height="1420" fill="#f6f7f9"/>',
 '<style>@font-face{font-family:UE;src:url(data:font/ttf;base64,'+font+')}text{fill:#182027;font-family:Arial,Helvetica,sans-serif}.serif{font-family:UE,Georgia,serif}.muted{fill:#53616d}.blue{fill:#1664a3}.white{fill:#fff}.kicker{font-size:12px;letter-spacing:2px}.study{font-size:18px;font-weight:600}.body{font-size:16px}.org{font-size:18px;font-weight:600}.number{fill:#9a7928;font-size:13px;letter-spacing:1px}</style>']
def text(x,y,t,cls='',size=None,extra=''):
 svg.append(f'<text x="{x}" y="{y}" class="{cls}"'+(f' font-size="{size}"' if size else '')+(' '+extra if extra else '')+'>'+escape(t)+'</text>')
def line(x1,y1,x2,y2,color='#d5dce3'):
 svg.append(f'<path d="M{x1} {y1}H{x2}" stroke="{color}"/>' if y1==y2 else f'<path d="M{x1} {y1}L{x2} {y2}" stroke="{color}"/>')
def wrapped(x,y,value,width,cls='body muted',leading=22):
 for j,t in enumerate(textwrap.wrap(value,width)):text(x,y+j*leading,t,cls)
text(48,49,'UNIVERSAL EVIDENCE  /  FIELD NOTES','kicker muted')
line(48,74,103,74,'#c9a547')
text(48,137,'Stunting: from evidence to action','serif',58)
text(48,176,'Follow the connections from a condition to research and related work.','muted',20)
# The tinted organization lane is distinct from the research column: proximity is not attribution.
svg.append('<rect x="1030" y="218" width="362" height="1002" rx="8" fill="#edf1f4"/>')
text(240,248,'INTERVENTION GROUPS','kicker muted')
text(535,248,'SELECTED EVIDENCE','kicker blue')
text(1054,248,'ORGANIZATIONS IN RELATED WORK','kicker muted')
line(240,268,1004,268)
line(1054,268,1368,268)
# A shared trunk keeps the connection paths legible.
node_x,node_y=108,746
line(178,node_y,203,node_y,'#c9a547')
line(203,366,203,1126,'#c9a547')
svg.append(f'<circle cx="{node_x}" cy="{node_y}" r="70" fill="#1664a3"/>')
text(node_x,node_y,'Stunting','serif white',31,'text-anchor="middle" dy=".3em"')
text(node_x,node_y+99,'THE CONDITION','kicker muted',extra='text-anchor="middle"')
for i,(heading,sub,studies,orgs) in enumerate(rows):
 y=292+i*190
 cy=y+74
 line(203,cy,225,cy,'#c9a547')
 svg.append(f'<circle cx="225" cy="{cy}" r="3" fill="#c9a547"/>')
 text(240,y+7,f'0{i+1}','number')
 text(240,y+47,heading[0],'serif',32)
 text(240,y+79,heading[1],'serif',32)
 
 if i==3:
  text(240,y+110,'Water treatment · sanitation','body muted')
  text(240,y+132,'Hygiene','body muted')
 else:wrapped(240,y+110,sub,29)
 for j,(name,lines) in enumerate(studies):
  yy=y+8+j*88
  text(535,yy,name,'study blue')
  # Descriptions and findings use separate lines, preserving each measure and qualification.
  for k,t in enumerate(lines):wrapped(535,yy+25+k*23,t,56,leading=20)
 for j,(name,description) in enumerate(orgs):
  yy=y+8+j*88
  svg.append('<a href="'+escape(links[name],quote=True)+'" target="_blank" rel="noopener noreferrer">')
  text(1054,yy,name,'org blue')
  svg.append('</a>')
  wrapped(1054,yy+26,description,34)
 if i<4:
  line(240,y+166,1004,y+166)
  line(1054,y+166,1368,y+166,'#d6dfe6')
line(48,1253,1392,1253)
text(48,1289,'HOW TO READ THIS MAP','kicker blue')
wrapped(48,1321,'Findings are not pooled estimates. Studies differ in design, population and outcome. Growth gains do not necessarily mean fewer children with stunting.',81)
wrapped(820,1321,'Organizations illustrate related work, not necessarily delivery of the adjacent studies. See the article for sources and qualifications.',67)
svg.append('</svg>')
p=root/'site/public/figures/stunting-evidence-map.svg'
p.write_text('\n'.join(svg));print(p)
