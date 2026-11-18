import {curveSegments,valueText} from './result-logic.js';

const NS='http://www.w3.org/2000/svg';
const count=value=>Number.isInteger(value)&&value>=0?value:null;
const number=value=>Number.isFinite(value)?value.toLocaleString('zh-CN',{maximumSignificantDigits:4}):'—';
const columnName=value=>{let name='';while(value>0){value--;name=String.fromCharCode(65+value%26)+name;value=Math.floor(value/26);}return name;};
const sourceAddress=source=>[source?.row,source?.column].every(n=>Number.isInteger(n)&&n>0&&n<=1000000)
  ?`${columnName(source.column)}${source.row}`:null;

/** Source-order curve display shared by deterministic preview and saved results. */
export function renderStandardCurve(row,{document:doc=globalThis.document,onSource=null}={}){
  const node=(tag,cls,text)=>{const el=doc.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=String(text);return el;};
  const svgNode=(tag,attributes,text)=>{const el=doc.createElementNS(NS,tag);for(const [name,value] of Object.entries(attributes||{}))el.setAttribute(name,value);if(text!==undefined)el.textContent=String(text);return el;};
  const preview=row.series_preview||{},figure=node('section','standard-curve');
  const xLabel=preview.x_is_point_index?'原始点序号':valueText(preview.x_label??'横轴',80);
  const yLabel=valueText(preview.y_label??row.field??'纵轴',80);
  const axis=(label,unit,index=false)=>`${label}${index?'':unit==null||unit===''?'（单位未注明）':`（${valueText(unit,50)}）`}`;
  figure.append(node('p','standard-curve-meta',`${axis(xLabel,preview.x_unit,preview.x_is_point_index)} · ${axis(yLabel,preview.y_unit)}`));
  const segments=curveSegments(preview),points=segments.flat();
  if(points.length){
    let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
    for(const [x,y] of points){minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);}
    // Scaling before subtraction avoids overflow for finite opposite extremes.
    const scale=(min,max,start,size)=>{const divisor=Math.max(Math.abs(min),Math.abs(max))||1,low=min/divisor,range=max/divisor-low;
      return value=>min===max?start+size/2:start+(value/divisor-low)/range*size;};
    const x=scale(minX,maxX,52,428),y=scale(minY,maxY,160,-140);
    const svg=svgNode('svg',{viewBox:'0 0 520 196',width:'100%',height:196,role:'img',
      'aria-label':`${yLabel}曲线预览，按来源顺序，缺失处断开`,class:'standard-curve-chart'});
    svg.append(svgNode('title',{},'按来源顺序连接已知数值；保留零值，缺失处不连线'));
    svg.append(svgNode('path',{d:'M52 20 V160 H480',fill:'none',stroke:'currentColor','stroke-width':0.7,opacity:0.35}));
    for(const segment of segments){
      if(segment.length===1)svg.append(svgNode('circle',{cx:x(segment[0][0]),cy:y(segment[0][1]),r:2,fill:'currentColor'}));
      else svg.append(svgNode('polyline',{points:segment.map(p=>`${x(p[0])},${y(p[1])}`).join(' '),
        fill:'none',stroke:'currentColor','stroke-width':1.6,'vector-effect':'non-scaling-stroke'}));
    }
    for(const [value,left,top,anchor] of [[minX,52,180,'start'],[maxX,480,180,'end'],[minY,46,163,'end'],[maxY,46,23,'end']])
      svg.append(svgNode('text',{x:left,y:top,'text-anchor':anchor,'font-size':10,fill:'currentColor'},number(value)));
    figure.append(svg);
  }else figure.append(node('p','standard-curve-empty','暂无可绘制数值'));
  const total=count(preview.total_points)??count(row.point_count),shown=Array.isArray(preview.points)?preview.points.length:0;
  const counters=[total===null?'原始点数未提供':`${total} 个原始点`];
  if(preview.sampled)counters.push(`预览 ${shown} 点`);
  if(count(preview.unplotted_points))counters.push(`${preview.unplotted_points} 个未绘制`);
  figure.append(node('p','standard-curve-meta',counters.join(' · ')));
  const issues=Array.isArray(preview.point_issues)?preview.point_issues:[],issueTotal=count(preview.point_issue_total)??issues.length;
  if(issueTotal||issues.length){
    const details=node('details','standard-curve-issues');details.append(node('summary','',`${issueTotal} 项数值待核对`));
    const list=node('ul');
    for(const issue of issues){
      const address=sourceAddress(issue),axisLabel=issue.axis==='x'?'横轴':'结果';
      const status=issue.kind==='formula_cached'?'公式值待核对':issue.status==='ambiguous'?'数字格式待核对':'数值未解析';
      const place=address?`${issue.sheet?`${issue.sheet} · `:''}${address}`:`第 ${(count(issue.point_index)??0)+1} 点`;
      const label=`${axisLabel} ${place}：${valueText(issue.raw,100)}${issue.raw_truncated?'…':''}（${status}）`;
      const item=node('li');
      if(typeof onSource==='function'&&address&&typeof issue.asset_ref==='string'&&issue.asset_ref){
        const button=node('button','button quiet',label);button.type='button';button.addEventListener('click',()=>onSource(row,issue));item.append(button);
      }else item.textContent=label;
      list.append(item);
    }
    details.append(list);
    if(issueTotal>issues.length)details.append(node('p','standard-curve-meta',`显示 ${issues.length} / ${issueTotal} 项`));
    figure.append(details);
  }
  return figure;
}
