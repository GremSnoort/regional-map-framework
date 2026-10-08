// Small, dependency-free SpreadsheetML workbook for table downloads.
// https://learn.microsoft.com/office/open-xml/spreadsheet/overview
const encoder=new TextEncoder();
const xml=value=>{
  const text=String(value??'');
  if(text.length>32767)throw new Error('Текст в одной из ячеек превышает допустимую длину Excel (32767 символов).');
  // Escape literal SpreadsheetML codes before encoding XML control characters.
  return text.replace(/_x[0-9a-f]{4}_/gi,code=>'_x005F_'+code.slice(1))
    .replace(/[\u0000-\u0008\u000b-\u000d\u000e-\u001f\ufffe\uffff]/g,char=>'_x'+char.charCodeAt(0).toString(16).toUpperCase().padStart(4,'0')+'_')
    .replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[char]));
};
const declaration='<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';
const namespace='http://schemas.openxmlformats.org/spreadsheetml/2006/main';
const relationship='http://schemas.openxmlformats.org/officeDocument/2006/relationships';
function columnName(index){let name='';for(index++;index;index=Math.floor((index-1)/26))name=String.fromCharCode(65+(index-1)%26)+name;return name}
function cell(value,reference,style=0){
  if(value===null||value===undefined)return `<c r="${reference}" s="${style}"/>`;
  if(typeof value==='number')return Number.isFinite(value)?`<c r="${reference}" s="${style}"><v>${value}</v></c>`:`<c r="${reference}" s="${style}"/>`;
  return `<c r="${reference}" s="${style}" t="inlineStr"><is><t xml:space="preserve">${xml(value)}</t></is></c>`;
}
function worksheet(rows,widths,{filter=false,formats=[]}={}){
  const end=`${columnName(widths.length-1)}${rows.length}`;
  const views=filter?'<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>':'';
  const columns=widths.map((width,index)=>`<col min="${index+1}" max="${index+1}" width="${width}" customWidth="1"/>`).join('');
  const data=rows.map((row,index)=>`<row r="${index+1}"${index===0?' ht="30" customHeight="1"':''}>${row.map((value,col)=>cell(value,`${columnName(col)}${index+1}`,index===0?1:typeof value==='number'?(formats[col]==='decimal'?3:2):4)).join('')}</row>`).join('');
  return declaration+`<worksheet xmlns="${namespace}"><dimension ref="A1:${end}"/>${views}<sheetFormatPr defaultRowHeight="18"/><cols>${columns}</cols><sheetData>${data}</sheetData>${filter?`<autoFilter ref="A1:${end}"/>`:''}</worksheet>`;
}
const styles=declaration+`<styleSheet xmlns="${namespace}"><numFmts count="2"><numFmt numFmtId="164" formatCode="#,##0.###"/><numFmt numFmtId="165" formatCode="#,##0.##"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF176636"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`;

const crcTable=Uint32Array.from({length:256},(_,n)=>{for(let bit=0;bit<8;bit++)n=(n&1)?0xedb88320^(n>>>1):n>>>1;return n>>>0});
function crc32(bytes){let crc=0xffffffff;for(const byte of bytes)crc=crcTable[(crc^byte)&255]^(crc>>>8);return (crc^0xffffffff)>>>0}
function zip(files){
  const parts=[],directory=[];let offset=0,directorySize=0;
  for(const [path,text] of Object.entries(files)){
    const name=encoder.encode(path),bytes=encoder.encode(text),crc=crc32(bytes);
    const local=new Uint8Array(30+name.length),view=new DataView(local.buffer);
    view.setUint32(0,0x04034b50,true);view.setUint16(4,20,true);view.setUint16(12,33,true);view.setUint32(14,crc,true);view.setUint32(18,bytes.length,true);view.setUint32(22,bytes.length,true);view.setUint16(26,name.length,true);local.set(name,30);
    const central=new Uint8Array(46+name.length),entry=new DataView(central.buffer);
    entry.setUint32(0,0x02014b50,true);entry.setUint16(4,20,true);entry.setUint16(6,20,true);entry.setUint16(14,33,true);entry.setUint32(16,crc,true);entry.setUint32(20,bytes.length,true);entry.setUint32(24,bytes.length,true);entry.setUint16(28,name.length,true);entry.setUint32(42,offset,true);central.set(name,46);
    parts.push(local,bytes);directory.push(central);offset+=local.length+bytes.length;directorySize+=central.length;
  }
  const end=new Uint8Array(22),view=new DataView(end.buffer);view.setUint32(0,0x06054b50,true);view.setUint16(8,directory.length,true);view.setUint16(10,directory.length,true);view.setUint32(12,directorySize,true);view.setUint32(16,offset,true);
  return new Blob([...parts,...directory,end],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
}

export function tableWorkbook({headers,rows,formats=[],metadata=[]}){
  if(!headers.length||headers.length>16384||rows.length>1048575)throw new Error('Table exceeds Excel worksheet limits');
  const widths=headers.map((header,index)=>Math.min(50,rows.reduce((width,row)=>Math.max(width,typeof row[index]==='number'?16:String(row[index]??'').length+2),Math.max(12,String(header).length+2))));
  const sheets=[['Таблица',worksheet([headers,...rows],widths,{filter:true,formats})],['Описание',worksheet([['Параметр','Значение'],...metadata],[26,100])]];
  const files={
    '[Content_Types].xml':declaration+`<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>${sheets.map((_,i)=>`<Override PartName="/xl/worksheets/sheet${i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join('')}</Types>`,
    '_rels/.rels':declaration+`<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="${relationship}/officeDocument" Target="xl/workbook.xml"/></Relationships>`,
    'xl/workbook.xml':declaration+`<workbook xmlns="${namespace}" xmlns:r="${relationship}"><sheets>${sheets.map(([name],i)=>`<sheet name="${name}" sheetId="${i+1}" r:id="rId${i+1}"/>`).join('')}</sheets></workbook>`,
    'xl/_rels/workbook.xml.rels':declaration+`<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">${sheets.map((_,i)=>`<Relationship Id="rId${i+1}" Type="${relationship}/worksheet" Target="worksheets/sheet${i+1}.xml"/>`).join('')}<Relationship Id="styles" Type="${relationship}/styles" Target="styles.xml"/></Relationships>`,
    'xl/styles.xml':styles,
  };
  sheets.forEach(([,sheet],i)=>{files[`xl/worksheets/sheet${i+1}.xml`]=sheet});return zip(files);
}
