"""Explicit prototype CLI; model-generated plans are not automatically trusted."""
import argparse
import json
import os
from .workflow import Workflow
from .opencode import OpenCode

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--db',default='assr.db')
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('create');a.add_argument('task');a.add_argument('role')
    a=sub.add_parser('step');a.add_argument('task');a.add_argument('text')
    a=sub.add_parser('ask');a.add_argument('question');a.add_argument('task');a.add_argument('text')
    a=sub.add_parser('answer');a.add_argument('question');a.add_argument('actor');a.add_argument('revision',type=int);a.add_argument('text')
    a=sub.add_parser('escalate');a.add_argument('question');a.add_argument('actor');a.add_argument('revision',type=int)
    a=sub.add_parser('plan');a.add_argument('task');a.add_argument('text')
    a=sub.add_parser('admit');a.add_argument('task');a.add_argument('digest');a.add_argument('actor')
    sub.add_parser('events')
    args=p.parse_args();w=Workflow(args.db)
    try:
        if args.command=='create':w.create(args.task,args.role)
        elif args.command=='step':
            runtime=OpenCode(os.getenv('ASSR_OPENCODE_URL','http://127.0.0.1:4096'),os.getenv('OPENCODE_SERVER_PASSWORD'))
            print(json.dumps(w.step(args.task,runtime,args.text),ensure_ascii=False))
        elif args.command=='ask':w.ask(args.question,args.task,args.text)
        elif args.command in ('answer','escalate'):w.resolve(args.question,args.actor,args.revision,getattr(args,'text',None))
        elif args.command=='plan':print(w.propose(args.task,args.text))
        elif args.command=='admit':w.admit(args.task,args.digest,args.actor)
        elif args.command=='events':print(json.dumps(w.history(),ensure_ascii=False,indent=2))
    finally:w.db.close()

if __name__=='__main__':main()
