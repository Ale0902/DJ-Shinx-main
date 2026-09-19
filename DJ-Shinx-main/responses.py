import random

def rolld6():
    dice = random.randint(1,6)
    return f'You rolled a {dice}!'

def rolld20():
    dice = random.randint(1,20)
    if dice == 20:
        return 'You rolled a natural 20!'
    else:
        return f'you rolled a {dice}!'

def ping():
    return 'PONG!'

def coinflip():
    coin = random.randint(1,2)
    if coin == 1:
        return 'Heads!'
    else:
        return 'Tails!'