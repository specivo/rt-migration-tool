#!/usr/bin/env perl
# Fill the fixture's RT with content that exercises every mapping of the tool,
# through RT's own Perl API, the same way on RT 4.4, 5.0 and 6.0.
#
# Run once in a fresh fixture (`make fixture-seed`). The end-to-end test
# (tests/test_e2e_fixture.py) checks the bundle against what this creates, by
# name: change one, change the other.
use strict;
use warnings;
use utf8;
use lib '/opt/rt/lib';

use RT;
RT::LoadConfig();
RT::Init();

use Encode qw(encode);
use MIME::Entity;
use MIME::Base64 qw(decode_base64);

binmode STDOUT, ':encoding(UTF-8)';

my $SYSTEM  = RT->SystemUser;
my $VERSION = $RT::VERSION;
my ($MAJOR) = $VERSION =~ /^(\d+\.\d+)/;
my $NEWER   = $MAJOR >= 5.0;
my $SIX     = $MAJOR >= 6.0;
print "Seeding RT $VERSION\n";

sub must {
    my ( $what, $ok, $msg ) = @_;
    die "$what failed: " . ( $msg // 'no message' ) . "\n" unless $ok;
    return $ok;
}

# A 1x1 PNG, and a small PDF-like binary file.
my $PNG = decode_base64('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==');
my $PDF = "%PDF-1.4\n" . join( '', map { chr( $_ % 256 ) } 0 .. 5000 ) . "\n%%EOF\n";

# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------

sub user {
    my (%args) = @_;
    my $user = RT::User->new($SYSTEM);
    must( "user $args{Name}", $user->Create(%args) );
    return $user;
}

my $alice = user( Name => 'alice', RealName => 'Alice Example', EmailAddress => 'alice@example.com', Privileged => 1 );
my $bob   = user( Name => 'bob', RealName => 'Bob Example', EmailAddress => 'bob@example.com', Privileged => 1 );
my $somchai = user(
    Name         => 'somchai',
    RealName     => 'สมชาย ใจดี',
    EmailAddress => 'somchai@example.com',
    Privileged   => 1,
    Lang         => 'th',
);
my $carol = user( Name => 'carol', RealName => 'Carol Example', EmailAddress => 'carol@example.com', Privileged => 1 );
must( 'disable carol', $carol->SetDisabled(1) );
my $dana = user( Name => 'dana@example.net', RealName => 'Dana Customer', EmailAddress => 'dana@example.net' );
my $eve  = user( Name => 'eve@example.org', EmailAddress => 'eve@example.org' );

my $root = RT::User->new($SYSTEM);
$root->Load('root');
must( 'alice is SuperUser', $alice->PrincipalObj->GrantRight( Right => 'SuperUser', Object => RT->System ) );

sub group {
    my ( $name, @members ) = @_;
    my $group = RT::Group->new($SYSTEM);
    must( "group $name", $group->CreateUserDefinedGroup( Name => $name, Description => "$name (fixture)" ) );
    must( "member of $name", $group->AddMember( $_->PrincipalId ) ) for @members;
    return $group;
}

my $support_team = group( 'Support Team', $alice, $bob );
my $thai_desk    = group( 'Thai Desk', $somchai );
my $all_agents   = group( 'All Agents', $support_team );

# --------------------------------------------------------------------------
# Queues and lifecycles
# --------------------------------------------------------------------------

if ($NEWER) {
    # A lifecycle of our own, kept in the database as RT 5.0 and later do.
    my %lifecycles = %{ RT->Config->Get('Lifecycles') };
    $lifecycles{billing} = {
        initial  => ['quoted'],
        active   => [ 'invoiced', 'overdue' ],
        inactive => [ 'paid', 'written-off' ],
        defaults => { on_create => 'quoted' },
        transitions => {
            ''            => [qw(quoted invoiced)],
            quoted        => [qw(invoiced written-off)],
            invoiced      => [qw(overdue paid written-off)],
            overdue       => [qw(paid written-off)],
            paid          => [qw(invoiced)],
            'written-off' => [qw(invoiced)],
        },
        rights => { '* -> *' => 'ModifyTicket' },
    };
    # How a ticket's status changes when it moves between the two lifecycles.
    $lifecycles{__maps__} = {
        %{ $lifecycles{__maps__} || {} },
        'default -> billing' => {
            new => 'quoted', open => 'invoiced', stalled => 'overdue',
            resolved => 'paid', rejected => 'written-off', deleted => 'written-off',
        },
        'billing -> default' => {
            quoted => 'new', invoiced => 'open', overdue => 'stalled', paid => 'resolved', 'written-off' => 'rejected',
        },
    };
    my $setting = RT::Configuration->new($SYSTEM);
    must( 'Lifecycles setting', $setting->Create( Name => 'Lifecycles', Content => \%lifecycles ) );
    my $priorities = RT::Configuration->new($SYSTEM);
    must(
        'PriorityAsString setting',
        $priorities->Create(
            Name    => 'PriorityAsString',
            Content => { Default => { Low => 0, Medium => 50, High => 100 }, Billing => [ Routine => 0, Urgent => 70 ] },
        )
    );
    RT->Config->LoadConfigFromDatabase();
    RT::Lifecycle->FillCache;
}

sub queue {
    my (%args) = @_;
    my $queue = RT::Queue->new($SYSTEM);
    must( "queue $args{Name}", $queue->Create(%args) );
    return $queue;
}

my $general = RT::Queue->new($SYSTEM);
$general->Load('General');
my $support = queue( Name => 'Support', Description => 'Customer support', CorrespondAddress => 'support@example.com' );
my $billing = queue( Name => 'Billing', Description => 'Invoices and payments', ( $NEWER ? ( Lifecycle => 'billing' ) : () ) );
my $old     = queue( Name => 'Old Projects', Description => 'Closed down' );
my $thai    = queue( Name => 'งานภาษาไทย', Description => 'คิวสำหรับลูกค้าภาษาไทย' );

# --------------------------------------------------------------------------
# Rights
# --------------------------------------------------------------------------

sub grant {
    my ( $principal, $object, @rights ) = @_;
    must( "grant @rights", $principal->PrincipalObj->GrantRight( Right => $_, Object => $object ) ) for @rights;
}

my @staff_rights = qw(SeeQueue ShowTicket ShowTicketComments CreateTicket ReplyToTicket CommentOnTicket
    OwnTicket ModifyTicket TakeTicket StealTicket Watch WatchAsAdminCc ModifyCustomField SeeCustomField);
grant( $support_team, $support, @staff_rights );
grant( $support_team, $billing, @staff_rights );
grant( $thai_desk, $thai, qw(SeeQueue ShowTicket) );
grant( $bob, $billing, 'AdminQueue' );
grant( $somchai, $old, qw(SeeQueue ShowTicket ShowTicketComments CreateTicket ReplyToTicket CommentOnTicket OwnTicket ModifyTicket) );

my $everyone = RT::Group->new($SYSTEM);
$everyone->LoadSystemInternalGroup('Everyone');
grant( $everyone, RT->System, qw(CreateTicket ReplyToTicket SeeCustomField) );

# Every privileged user may see General's tickets: one group of them, a Reporter there.
my $privileged = RT::Group->new($SYSTEM);
$privileged->LoadSystemInternalGroup('Privileged');
grant( $privileged, $general, qw(SeeQueue ShowTicket) );

# The AdminCc role of Support may change its tickets; somchai is one of its AdminCcs.
my $support_admincc = RT::Group->new($SYSTEM);
$support_admincc->LoadRoleGroup( Object => $support, Name => 'AdminCc' );
must( 'grant AdminCc', $support_admincc->PrincipalObj->GrantRight( Right => $_, Object => $support ) )
    for qw(ShowTicket ModifyTicket ReplyToTicket CommentOnTicket SeeQueue);
must( 'queue AdminCc', $support->AddWatcher( Type => 'AdminCc', PrincipalId => $somchai->PrincipalId ) );
# The Requestor of each ticket may see it: a per-ticket role, counted.
my $requestor_role = RT::Group->new($SYSTEM);
$requestor_role->LoadRoleGroup( Object => RT->System, Name => 'Requestor' );
must( 'grant Requestor', $requestor_role->PrincipalObj->GrantRight( Right => 'ShowTicket', Object => RT->System ) );

# --------------------------------------------------------------------------
# Custom fields
# --------------------------------------------------------------------------

sub custom_field {
    my ( $queues, %args ) = @_;
    my $cf = RT::CustomField->new($SYSTEM);
    must( "custom field $args{Name}", $cf->Create( LookupType => 'RT::Queue-RT::Ticket', %args ) );
    for my $queue (@$queues) {
        must( "apply $args{Name}", $cf->AddToObject($queue) );
    }
    return $cf;
}

my $global = RT::Queue->new($SYSTEM);    # id 0: every queue
my $severity = custom_field( [$support], Name => 'Severity', Type => 'Select', MaxValues => 1 );
$severity->AddValue( Name => $_->[0], SortOrder => $_->[1] ) for [ Low => 1 ], [ High => 2 ], [ Critical => 3 ];
my $components = custom_field( [ $support, $billing ], Name => 'Components', Type => 'Select', MaxValues => 0 );
$components->AddValue( Name => $_ ) for qw(Web Mobile API);
my $reference = custom_field( [$global], Name => 'Customer reference', Type => 'Freeform', MaxValues => 1 );
my $notes     = custom_field( [$support], Name => 'Notes', Type => 'Text', MaxValues => 1 );
my $runbook   = custom_field( [$support], Name => 'Runbook', Type => 'Wikitext', MaxValues => 1 );
my $golive    = custom_field( [$support], Name => 'Go-live date', Type => 'Date', MaxValues => 1 );
my $window    = custom_field( [$support], Name => 'Maintenance window', Type => 'DateTime', MaxValues => 1 );
my $address   = custom_field( [$support], Name => 'Server address', Type => 'IPAddress', MaxValues => 1 );
my $range     = custom_field( [$support], Name => 'Network range', Type => 'IPAddressRange', MaxValues => 1 );
my $product   = custom_field( [$support], Name => 'Product', Type => 'Autocomplete', MaxValues => 1 );
$product->AddValue( Name => $_ ) for ( 'Tracker', 'Wiki' );
my $tags        = custom_field( [$support], Name => 'Labels', Type => 'Freeform', MaxValues => 0 );
my $screenshot  = custom_field( [$support], Name => 'Screenshot', Type => 'Image', MaxValues => 1 );
my $contract    = custom_field( [$billing], Name => 'Contract', Type => 'Binary', MaxValues => 1 );
my $thai_client = custom_field( [$thai], Name => 'ลูกค้า', Type => 'Freeform', MaxValues => 1 );

# Custom fields on other things: counted, not carried.
my $cost_centre = RT::CustomField->new($SYSTEM);
must( 'queue cf', $cost_centre->Create( Name => 'Cost centre', Type => 'Freeform', MaxValues => 1, LookupType => 'RT::Queue' ) );
must( 'apply queue cf', $cost_centre->AddToObject( RT::Queue->new($SYSTEM) ) );
must( 'queue cf value', $support->AddCustomFieldValue( Field => $cost_centre->id, Value => 'Operations 100' ) );
my $department = RT::CustomField->new($SYSTEM);
must( 'user cf', $department->Create( Name => 'Department', Type => 'Freeform', MaxValues => 1, LookupType => 'RT::User' ) );
must( 'apply user cf', $department->AddToObject( RT::User->new($SYSTEM) ) );
must( 'user cf value', $bob->AddCustomFieldValue( Field => $department->id, Value => 'Operations' ) );

# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------

sub as {
    my ( $user, $ticket_id ) = @_;
    my $ticket = RT::Ticket->new( RT::CurrentUser->new($user) );
    $ticket->Load($ticket_id);
    return $ticket;
}

sub mime {
    my (%args) = @_;
    my $top = MIME::Entity->build(
        Type    => $args{Type} // 'text/plain',
        Charset => $args{Charset} // 'UTF-8',
        Subject => encode( 'MIME-Header', $args{Subject} // 'Fixture message' ),
        From    => $args{From} // 'dana@example.net',
        To      => $args{To} // 'support@example.com',
        ( $args{Cc} ? ( Cc => $args{Cc} ) : () ),
        'Message-ID' => $args{MessageId} // '<' . rand() . '@fixture.example.com>',
        ( defined $args{Data} ? ( Data => [ encode( $args{Charset} // 'UTF-8', $args{Data} ) ] ) : () ),
        ( $args{Encoding} ? ( Encoding => $args{Encoding} ) : () ),
    );
    return $top;
}

sub new_ticket {
    my ( $user, %args ) = @_;
    my $ticket = RT::Ticket->new( RT::CurrentUser->new($user) );
    my ( $id, undef, $msg ) = $ticket->Create(%args);
    must( "ticket $args{Subject}", $id, $msg );
    return $id;
}

# 1. A support request by mail: HTML and plain text alternatives, an inline
# image, a PDF, Thai text, a requestor who is not privileged.
my $first = MIME::Entity->build(
    Type         => 'multipart/mixed',
    Subject      => 'Login page is broken',
    From         => 'Dana Customer <dana@example.net>',
    To           => 'support@example.com',
    Cc           => 'eve@example.org',
    'Message-ID' => '<first-request@example.net>',
);
my $alternative = MIME::Entity->build( Type => 'multipart/alternative', Top => 0 );
$alternative->attach(
    Type    => 'text/plain',
    Charset => 'UTF-8',
    Data    => [ encode( 'UTF-8', "The login page shows an error.\nภาษาไทย: เข้าสู่ระบบไม่ได้\n" ) ],
);
my $related = MIME::Entity->build( Type => 'multipart/related', Top => 0 );
$related->attach(
    Type    => 'text/html',
    Charset => 'UTF-8',
    Data    => [
        encode(
            'UTF-8',
            '<p>The <b>login page</b> shows an error.</p><p>ภาษาไทย: เข้าสู่ระบบไม่ได้</p>'
                . '<p><img src="cid:screenshot-1@example.net" alt="the error"></p>'
                . '<script>alert(1)</script>'
        )
    ],
);
$related->attach(
    Type        => 'image/png',
    Data        => [$PNG],
    Filename    => 'error.png',
    Disposition => 'inline',
    Id          => '<screenshot-1@example.net>',
    Encoding    => 'base64',
);
$alternative->add_part($related);
$first->add_part($alternative);
$first->attach( Type => 'application/pdf', Data => [$PDF], Filename => 'invoice.pdf', Encoding => 'base64' );

my $t1 = new_ticket(
    $dana,
    Queue          => $support->id,
    Subject        => 'Login page is broken',
    Requestor      => [ $dana->EmailAddress ],
    Cc             => [ $eve->EmailAddress ],
    MIMEObj        => $first,
    Priority       => 50,
    InitialPriority => 20,
    FinalPriority  => 90,
    TimeEstimated  => 120,
    TimeLeft       => 90,
    Due            => '2026-11-20 10:00:00',
    Starts         => '2026-11-02 09:00:00',
);
print "Ticket $t1: Login page is broken\n";

my $t2 = new_ticket( $bob, Queue => $support->id, Subject => 'Password reset mail is late', Owner => $bob->id,
    MIMEObj => mime( Data => "Reset mails take an hour.\n", From => 'bob@example.com' ) );
my $t3 = new_ticket( $bob, Queue => $billing->id, Subject => 'Invoice for the login fix',
    MIMEObj => mime( Data => "Bill the work on RT #$t1, see also ticket $t2.\n", From => 'bob@example.com' ) );
my $t4 = new_ticket( $alice, Queue => $support->id, Subject => 'Duplicate: cannot log in',
    MIMEObj => mime( Data => "Same as [fixture.example.com #$t1].\n", From => 'alice@example.com' ) );
my $t5 = new_ticket( $dana, Queue => $support->id, Subject => 'Still cannot log in', Requestor => [ $dana->EmailAddress ],
    MIMEObj => mime( Data => "Any news?\n", Subject => 'Still cannot log in' ) );
my $t6 = new_ticket( $alice, Queue => $support->id, Subject => 'Spam', MIMEObj => mime( Data => "Buy now\n" ) );
my $t7 = new_ticket( $alice, Queue => $support->id, Subject => 'Not our product', MIMEObj => mime( Data => "Wrong vendor.\n" ) );
my $t8 = new_ticket( $somchai, Queue => $old->id, Subject => 'Migrate the old server',
    MIMEObj => mime( Data => "Done long ago.\n", From => 'somchai@example.com' ) );
my $t9 = new_ticket(
    $somchai,
    Queue   => $thai->id,
    Subject => 'ใบแจ้งหนี้ไม่ถูกต้อง',
    MIMEObj => mime( Data => "ยอดเงินในใบแจ้งหนี้ไม่ถูกต้อง ดู ticket #$t3\n", Subject => 'ใบแจ้งหนี้ไม่ถูกต้อง' ),
);
my $t10 = new_ticket( $bob, Queue => $billing->id, Subject => 'Refund for order 1001',
    MIMEObj => mime( Data => "Refund requested.\n", From => 'bob@example.com' ) );

# Ticket 1: the conversation.
my $ticket = as( $bob, $t1 );
must( 'take', $ticket->Take );
must( 'status open', $ticket->SetStatus('open') );
my $reply = MIME::Entity->build( Type => 'multipart/alternative', Subject => 'Re: Login page is broken',
    From => 'bob@example.com', To => 'dana@example.net' );
$reply->attach( Type => 'text/plain', Charset => 'UTF-8', Data => ["We are on it.\n"] );
$reply->attach( Type => 'text/html', Charset => 'UTF-8',
    Data => [ '<p>We are <i>on it</i>. Tracking in <a href="https://www.example.com/status">the status page</a>.</p>' ] );
must( 'reply', $ticket->Correspond( MIMEObj => $reply, TimeTaken => 15 ) );

$ticket = as( $alice, $t1 );
my $comment = mime( From => 'alice@example.com', Data => "Internal: the session cookie expires early.\nSee ticket #$t4 too.\n" );
$comment->make_multipart;
$comment->attach( Type => 'text/plain', Data => ["session expired\nrenew failed\n"], Filename => 'server.log' );
must( 'comment', $ticket->Comment( MIMEObj => $comment, TimeTaken => 30 ) );

# A reply in Thai, in TIS-620 and quoted-printable, as mail clients send it.
$ticket = as( $dana, $t1 );
my $thai_mail = MIME::Entity->build(
    Type     => 'text/plain',
    Charset  => 'TIS-620',
    Encoding => 'quoted-printable',
    Subject  => 'Re: Login page is broken',
    From     => 'dana@example.net',
    Data     => [ encode( 'TIS-620', "ขอบคุณครับ ตอนนี้ใช้งานได้แล้ว\n" ) ],
);
must( 'thai reply', $ticket->Correspond( MIMEObj => $thai_mail ) );

$ticket = as( $alice, $t1 );
must( 'subject', $ticket->SetSubject('Login page is broken after the upgrade') );
must( 'priority', $ticket->SetPriority(80) );
must( 'due', $ticket->SetDue('2026-11-25 10:00:00') );
must( 'estimate', $ticket->SetTimeEstimated(180) );
must( 'stall', $ticket->SetStatus('stalled') );
must( 'give', $ticket->SetOwner( $alice->id, 'Steal' ) );
must( 'give back', $ticket->SetOwner( $bob->id, 'Give' ) );
must( 'add AdminCc', $ticket->AddWatcher( Type => 'AdminCc', PrincipalId => $somchai->PrincipalId ) );
must( 'add Cc group', $ticket->AddWatcher( Type => 'Cc', PrincipalId => $thai_desk->PrincipalId ) );
must( 'del Cc', $ticket->DeleteWatcher( Type => 'Cc', PrincipalId => $eve->PrincipalId ) );
must( 'move queue', $ticket->SetQueue( $billing->id ) );
must( 'move back', $ticket->SetQueue( $support->id ) );

# Custom fields, and a change of each kind.
must( 'severity', $ticket->AddCustomFieldValue( Field => $severity->id, Value => 'High' ) );
must( 'severity again', $ticket->AddCustomFieldValue( Field => $severity->id, Value => 'Critical' ) );
must( 'components', $ticket->AddCustomFieldValue( Field => $components->id, Value => $_ ) ) for qw(Web API);
must( 'components removed', $ticket->DeleteCustomFieldValue( Field => $components->id, Value => 'API' ) );
must( 'reference', $ticket->AddCustomFieldValue( Field => $reference->id, Value => 'cust0042' ) );
must( 'notes', $ticket->AddCustomFieldValue( Field => $notes->id, Value => "First line\nSecond line" ) );
must( 'runbook', $ticket->AddCustomFieldValue( Field => $runbook->id, Value => "Restart the *auth* service." ) );
must( 'go-live', $ticket->AddCustomFieldValue( Field => $golive->id, Value => '2026-12-01' ) );
must( 'window', $ticket->AddCustomFieldValue( Field => $window->id, Value => '2026-12-01 22:00:00' ) );
must( 'address', $ticket->AddCustomFieldValue( Field => $address->id, Value => '192.0.2.10' ) );
must( 'range', $ticket->AddCustomFieldValue( Field => $range->id, Value => '198.51.100.0-198.51.100.255' ) );
must( 'product', $ticket->AddCustomFieldValue( Field => $product->id, Value => 'Tracker' ) );
must( 'labels', $ticket->AddCustomFieldValue( Field => $tags->id, Value => $_ ) ) for ( 'login', 'urgent' );
must(
    'screenshot',
    $ticket->AddCustomFieldValue(
        Field        => $screenshot->id,
        Value        => 'login-error.png',
        LargeContent => $PNG,
        ContentType  => 'image/png',
    )
);

# Merged: ticket 5 into ticket 1, and ticket 4 into ticket 1.
$ticket = as( $dana, $t5 );
my $followup = mime( Data => "Here is another screenshot.\n" );
$followup->make_multipart;
$followup->attach( Type => 'image/png', Data => [$PNG], Filename => 'second.png', Encoding => 'base64' );
must( 'followup', $ticket->Correspond( MIMEObj => $followup ) );
$ticket = as( $alice, $t5 );
must( 'merge 5', $ticket->MergeInto($t1) );
$ticket = as( $alice, $t4 );
must( 'merge 4', $ticket->MergeInto($t1) );

# Links of each kind.
$ticket = as( $bob, $t3 );
must( 'member of', $ticket->AddLink( Type => 'MemberOf', Target => $t1 ) );
must( 'depends on', $ticket->AddLink( Type => 'DependsOn', Target => $t2 ) );
must( 'refers to', $ticket->AddLink( Type => 'RefersTo', Target => $t10 ) );
must( 'refers to url', $ticket->AddLink( Type => 'RefersTo', Target => 'https://www.example.com/spec' ) );
must( 'refers to merged', $ticket->AddLink( Type => 'RefersTo', Target => $t5 ) );
must( 'contract', $ticket->AddCustomFieldValue( Field => $contract->id, Value => 'contract.pdf', LargeContent => $PDF,
    ContentType => 'application/pdf' ) );
must( 'billing components', $ticket->AddCustomFieldValue( Field => $components->id, Value => 'Mobile' ) );
$ticket = as( $bob, $t10 );
must( 'delete link', $ticket->AddLink( Type => 'DependsOn', Target => $t2 ) );
must( 'delete link', $ticket->DeleteLink( Type => 'DependsOn', Target => $t2 ) );
if ($NEWER) {
    must( 'invoice 10', $ticket->SetStatus('invoiced') );
    must( 'pay 10', $ticket->SetStatus('paid') );
}
else {
    must( 'resolve 10', $ticket->SetStatus('resolved') );
}

# Time on ticket 2, by someone other than the one who records it on RT 6.0.
$ticket = as( $bob, $t2 );
must(
    'time',
    $ticket->Comment(
        Content   => "Investigated the mail queue.\n",
        TimeTaken => 45,
        ( $SIX ? ( TimeWorker => $alice->id, TimeWorkedDate => '2026-10-01' ) : () ),
    )
);
must( 'worked', $ticket->SetTimeWorked( $ticket->TimeWorked + 20 ) );

# Statuses: deleted (left out), rejected, resolved.
$ticket = as( $alice, $t6 );
must( 'delete 6', $ticket->SetStatus('deleted') );
$ticket = as( $alice, $t7 );
must( 'reject 7', $ticket->SetStatus('rejected') );
$ticket = as( $somchai, $t8 );
must( 'resolve 8', $ticket->SetStatus('resolved') );
must( 'thai cf', as( $alice, $t9 )->AddCustomFieldValue( Field => $thai_client->id, Value => 'บริษัท ตัวอย่าง จำกัด' ) );

# A reminder on ticket 1, and an approval.
$ticket = as( $alice, $t1 );
must( 'reminder', $ticket->Reminders->Add( Subject => 'Call the customer back', Owner => $bob->id, Due => '2026-11-03 09:00:00' ) );
my $approvals = RT::Queue->new($SYSTEM);
$approvals->Load('___Approvals');
new_ticket( $SYSTEM, Queue => $approvals->id, Type => 'approval', Subject => 'Approve the refund',
    MIMEObj => mime( Data => "Please approve.\n" ) );

# The old queue is closed down.
must( 'disable queue', $old->SetDisabled(1) );

# --------------------------------------------------------------------------
# Articles
# --------------------------------------------------------------------------

sub article_class {
    my ( $name, @queues ) = @_;
    my $class = RT::Class->new($SYSTEM);
    must( "class $name", $class->Create( Name => $name, Description => "$name (fixture)" ) );
    must( "apply $name", $class->AddToObject($_) ) for @queues;
    return $class;
}

my $kb  = article_class( 'Support Knowledge', $support );
my $faq = article_class( 'FAQ', RT::Queue->new($SYSTEM) );    # id 0: every queue
my $content_cf = RT::CustomField->new($SYSTEM);
$content_cf->LoadByName( Name => 'Content', LookupType => 'RT::Class-RT::Article' );
must( 'content cf', $content_cf->id );
must( "content on $_", $content_cf->AddToObject($_) ) for ( $kb, $faq );

grant( $support_team, $_, qw(SeeClass ShowArticle CreateArticle ModifyArticle ModifyCustomField SeeCustomField) )
    for ( $kb, $faq );

sub article {
    my ( $class, $name, $summary, @revisions ) = @_;
    my $article = RT::Article->new( RT::CurrentUser->new($alice) );
    my ($id, $msg) = $article->Create( Class => $class->id, Name => $name, Summary => $summary,
        'CustomField-' . $content_cf->id => shift @revisions );
    must( "article $name", $id, $msg );
    for my $text (@revisions) {
        my $editor = RT::Article->new( RT::CurrentUser->new($bob) );
        $editor->Load($id);
        must( "revise $name", $editor->AddCustomFieldValue( Field => $content_cf->id, Value => $text ) );
    }
    return $article;
}

article( $kb, 'Resetting a password', 'How to reset a password',
    '<p>Open the <b>login page</b> and choose <i>Forgot password</i>.</p>',
    '<p>Open the <b>login page</b>, choose <i>Forgot password</i> and check your mail.</p>',
);
article( $faq, 'วิธีติดต่อฝ่ายบริการ', 'ติดต่อเรา', '<p>โทร 02 000 0000</p>' );
my $retired = article( $faq, 'Fax numbers', 'No longer used', '<p>We do not use fax.</p>' );
must( 'retire article', RT::Article->new($SYSTEM)->Load( $retired->id ) );
{
    my $old_article = RT::Article->new($SYSTEM);
    $old_article->Load( $retired->id );
    must( 'disable article', $old_article->SetDisabled(1) );
}

# --------------------------------------------------------------------------
# Things the bundle does not carry
# --------------------------------------------------------------------------

my $asset = RT::Asset->new($SYSTEM);
must( 'asset', $asset->Create( Catalog => 'General assets', Name => 'Laptop 1' ) );
must( 'saved search', $bob->AddAttribute( Name => 'SavedSearch', Description => 'My open tickets',
    Content => { Query => "Owner = '__CurrentUser__' AND Status = 'open'" } ) );

# --------------------------------------------------------------------------
# External storage: move the large and binary files out, then add one more
# file, which stays in the database.
# --------------------------------------------------------------------------

system('/opt/rt/sbin/rt-externalize-attachments') == 0 or die "rt-externalize-attachments failed\n";
$ticket = as( $bob, $t2 );
my $late = mime( From => 'bob@example.com', Data => "Mail log attached.\n" );
$late->make_multipart;
$late->attach( Type => 'application/octet-stream', Data => ["\x00\x01binary log\xff"], Filename => 'mail.bin',
    Encoding => 'base64' );
must( 'late file', $ticket->Comment( MIMEObj => $late ) );

print "Seeded: tickets $t1 to $t10\n";
