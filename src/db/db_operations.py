import sqlalchemy as db
from sqlalchemy import exc
import sqlalchemy.orm
from datetime import timedelta, datetime

from contextlib import contextmanager
from db.db_helpers import *
from db.schema import \
    Event, EventTag, User, Club, ClubMembership, EventEmail, sqlengine
import db.schema as schema
import calendar

MAX_COMMIT_RETRIES = 10

# Set up SQLachemy sesion factory
session_factory = sqlalchemy.orm.sessionmaker(bind=sqlengine)  # main object used for queries
Session = sqlalchemy.orm.scoped_session(session_factory) #We use scoped_session for thread safety

@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = Session()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()



##############################################################
# Database Operations
##############################################################

## Permission Checking
def has_edit_permission(session, user_id, event_id):
    '''
    Check whether a user has permissions to edit/update an event
        
    '''
    #Get information about user
    user = session.query(User).filter(User.id==user_id).first()
    if not user:
        return False #No such user exists in db
    
    #Check if user is server admin (has permissions to edit all events)
    user_privilege = user.user_privilege
    if user_privilege == schema.UserPrivilege.ADMIN.value: 
        return True
    
    #Check if user is either submitter or
    #is an officer of the club that runs the event
    event = session.query(Event).filter(Event.id==event_id).first()
    if not event:
        return False #No such event
    if event.user_id == user_id:
        return True #User is person who submitted the event
    if not event.club_id:
        return False #Not submitter and event has no club associated with it
    
    is_officer = session.query(ClubMembership).filter(
            ClubMembership.user_id == user_id,
            ClubMembership.club_id == event.club_id,
            ClubMembership.member_privilege == schema.MemberPrivilege.OFFICER.value
        ).first() is not None
    return is_officer


## Get Functions
def get_all_events(session):
    '''Get information of all of events in database'''
    return session.query(Event).all()

def get_events_by_date(session, from_date, include_description=False):
    '''
    Get all events happening on a given day,
    ordered by start time and event name
    
    from_date: datetime Date object for target day
    '''
    query = session.query(
            Event
        ).filter(
            Event.start_date.between(from_date, from_date)
        ).order_by(
            Event.start_date, Event.start_time, Event.title
        )
    if include_description:
        # Include the description and description_html fields
        query = query.options(sqlalchemy.orm.undefer_group("full_description"))
    events = query.all()
    return events

def get_events_by_month(session, month,year=None):
    '''
    Get all events happening in a given month,
    ordered by start time and event name
    
    Month: int in range [1,12] inclusive
    Year: int (if None, will interpret as current year)

    '''
    assert 1 <= month <= 12, "Month must be in range 1 and 12"
    if not year:
        year = datetime.now().year
    _, last_day_of_month = calendar.monthrange(year,month)

    from_date = datetime(year,month,1).date()
    to_date = datetime(year,month,last_day_of_month).date()
    query = session.query(
            Event
        ).filter(
            Event.start_date.between(from_date, to_date)
        ).order_by(
            Event.start_date, Event.start_time, Event.title
        )
    events = query.all()
    return events

def get_event_tags(session, events):
    '''
    Given a list of Event models, return a list of all tags associated with each event
    '''
    res = []
    for event in events:
        #Using relationships defined in Event
        event_tags = [x.get_tag_value() for x in event.tags.all()]
        res.append(event_tags)
    return res

def get_event_user_emails(session, events):
    '''
    Given a list of Event models, return a list of the user email associated with each event (e.g. sender)
    '''
    res = []
    for event in events:
        #Using relationships defined in Event
        if event.user:
            res.append(event.user.get_user_email())
        else:
            res.append("unknown_user") # Default "unknown_user" is failsafe in case we misadd a user
    return res

## Add Functions
def add_to_db(session, obj, others=None,rollbackfunc=None):
    """Adds objects to database with re-trials
    
    Arguments:
        obj {Model} -- Object(s) wanting to add
    
    Keyword Arguments:
        others {List} -- List of other model objects (default: {None})
        rollbackfunc {Func} -- Function that should be called on rollback (default: {None})
    
    Returns:
        Boolean - Success or not successful
    """
    global MAX_COMMIT_RETRIES
    retry = MAX_COMMIT_RETRIES
    committed = False
    while (not committed and retry > 0):
        try:
            session.add(obj)
            if (others):
                for o in others:
                    session.add(o)
            session.commit()
        except exc.IntegrityError:
            session.rollback()
            if (rollbackfunc):
                rollbackfunc()
            else:
                retry = 0
            retry -= 1
        else:
            committed = True
    return committed

def add_event(session, title, user_id, description, event_tags=[0],\
              start_date=None, end_date=None, start_time=None, end_time=None, \
              description_html=None, club_id=None, location=None, cta_link=None):
    '''
    Adds an event to the database
    
    Returns id of new event, or None if add failed
    '''
    event = Event()
    #Required fields
    event.title = title
    event.user_id = user_id
    event.description = description
    #Optional Fields
    event.club_id = club_id
    event.description_html = description_html
    event.location = location
    event.start_date = start_date
    event.end_date = end_date
    event.start_time = start_time
    event.end_time = end_time
    event.cta_link = cta_link

    committed = add_to_db(session, event)
    if committed:
        session.flush()
        add_event_tags(session, event.id,event_tags)
        return event.id
    return None

def add_event_tags(session, event_id, event_tags):
    '''
    Given event_tags (list of ints representing enum Categories)
    and an event_id, link the event to those tags
    
    Note: Will delete existing event_tags if they exist
    '''
    #Delete existing event_tags
    session.query(EventTag).filter(
            EventTag.event_id==event_id
        ).delete()
    
    #Create new event tags
    new_tags = []
    for tag in event_tags:
        new_tags.append(EventTag(event_id,tag))
    session.add_all(new_tags)
    session.commit()
    
def add_user(session, email,user_privilege=0):
    '''
    Add a new user to the database (if it doesn't exist). 
    
    If user already exists with that email, returns id 
    of current user. Else, return id of new user, or None 
    if add failed.
    '''
    curr_user = session.query(User).filter(User.email==email).first()
    if curr_user:
        return curr_user.id
    
    new_user = User(email,user_privilege)
    committed = add_to_db(session, new_user)
    if committed:
        session.flush()
        return new_user.id
    return None

def add_club(session, club_name,club_abbrev=None,exec_email=None):
    '''
    Add a new club to the database (if it doesn't exist). 
    
    If a club already exists with that name, returns id 
    of current club. Else, return id of new club, or None
    if add failed.
    '''
    curr_club = session.query(Club).filter(Club.name==club_name).first()
    if curr_club:
        return curr_club.id
    
    new_club = Club(club_name,club_abbrev,exec_email)
    committed = add_to_db(session, new_club)
    
    if committed:
        session.flush()
        return new_club.id
    return None

def add_club_member(session, club_id,user_id,member_privilege=0):
    '''
    Add a user to be a member/officer of a club.
    Requires user_id and club_id to be valid User and Club ID.
    
    If User `user_id` is already part of Club `club_id`,
    update `member_privilege` to be highest between current and new.
    '''
    curr_membership = session.query(ClubMembership).filter(
                        ClubMembership.club_id==club_id,
                        ClubMembership.user_id==user_id,
                    ).first()
    if curr_membership:
        assert isinstance(curr_membership,ClubMembership)
        if member_privilege > curr_membership.member_privilege:
            curr_membership.member_privilege = member_privilege #Update privilege to be higher
            session.commit()
        return
    
    new_membership = ClubMembership(user_id,club_id,member_privilege)
    add_to_db(session, new_membership)
    
def add_event_email(session,event_id,message_id,in_reply_to):
    '''
    Add email header metadata for a given event with `event_id` 
    Requires event_id to be a valid Event ID.
    
    Assumes that there is no entry in the EventEmail table
    for this `event_id` already
    '''
    event_email = EventEmail(event_id,message_id,in_reply_to)
    add_to_db(session, event_email)

## Update functions

def update_event_description(session, event_id, description, description_html):
    '''
    Update descriptions for an existing event with id `event_id` in the database
    
    Returns whether update was successful
    '''
    event = session.query(Event).filter(Event.id==event_id).first()
    if not event: 
        return False #Event doesn't exist
    
    error=False
    # Attempt to make corresponding updates
    try: 
        event.description = description
        event.description_html = description_html
    except:
        session.rollback()
        error=True
    else:
        session.commit()
    
    return not error

def update_event(session, event_id, title, description, event_tags=None,\
                start_date=None, end_date=None, start_time=None, end_time=None, \
                description_html=None, club_id=None, location=None, cta_link=None):
    '''
    Update an existing event with id `event_id` in the database
    
    Note: Use this function if you are updating many different
    aspects of an event at once. Otherwise, it is preferable to
    use a more specific update function.
    
    Disallowed fields:
    - user_id, club_id
        - These fields should be updated separately with admin privileges
    
    Fields different add_event:
    - event_tags
        - Value of None indicates event_tags should not be updated
    
    Returns whether update was successful
    '''
    event = session.query(Event).filter(Event.id==event_id).first()
    if not event: 
        return False #Event doesn't exist
    
    error=False
    # Attempt to make corresponding updates
    try: 
        #Required fields
        event.title = title
        event.description = description
        #Optional Fields
        event.description_html = description_html
        event.location = location
        event.start_date = start_date
        event.end_date = end_date
        event.start_time = start_time
        event.end_time = end_time
        event.cta_link = cta_link
    except:
        session.rollback()
        error=True
    else:
        session.commit()

    #Update event tags if necessary
    if event_tags:
        add_event_tags(session, event_id, event_tags)
    
    return not error