from zipfile import ZipFile
from xml.etree import ElementTree as ET
import posixpath
import re

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'


def sheets(path):
    with ZipFile(path) as z:
        workbook=ET.fromstring(z.read('xl/workbook.xml'))
        relationships=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        targets={r.attrib['Id']:r.attrib['Target'] for r in relationships}
        return [(s.attrib['name'],posixpath.normpath('xl/'+targets[s.attrib[REL]]) if not targets[s.attrib[REL]].startswith('/') else targets[s.attrib[REL]].lstrip('/'))
                for s in workbook.find('s:sheets',NS)]


def rows(path,sheet_path):
    with ZipFile(path) as z:
        shared=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            root=ET.fromstring(z.read('xl/sharedStrings.xml'))
            shared=[''.join(t.text or '' for t in si.iter('{'+NS['s']+'}t')) for si in root]
        with z.open(sheet_path) as stream:
            for event,node in ET.iterparse(stream,events=['end']):
                if node.tag!='{'+NS['s']+'}row': continue
                result={}
                for cell in node:
                    ref=cell.attrib.get('r','A1')
                    letters=re.match('[A-Z]+',ref)[0]; col=0
                    for c in letters: col=col*26+ord(c)-64
                    typ=cell.attrib.get('t'); value=cell.find('s:v',NS)
                    if typ=='inlineStr':
                        val=''.join(t.text or '' for t in cell.iter('{'+NS['s']+'}t'))
                    elif value is None or value.text is None: val=None
                    elif typ=='s': val=shared[int(value.text)]
                    else: val=value.text
                    result[col-1]=val
                if result:
                    yield int(node.attrib.get('r',0)), [result.get(i) for i in range(max(result)+1)]
                node.clear()
