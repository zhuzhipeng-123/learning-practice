import json


def feedback_json(messages, comment='Observed answer'):
    context = json.loads(messages[1]['content'])
    turn = next(turn for turn in context['turns'] if turn['role'] == 'user')
    return json.dumps({'observations':[{'kind':'gap','turn_id':turn['id'],'quote':turn['content'][:100],'comment':comment}],
                       'next_steps':['Practice a concrete related case.']})
