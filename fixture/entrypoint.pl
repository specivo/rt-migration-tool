#!/usr/bin/env perl
# Start the fixture's RT container: wait for the database, create RT's schema
# and initial data once, then stay up so the seed script can run in it. The
# healthcheck waits for /tmp/rt-ready.
use strict;
use warnings;
use DBI;

my $type = $ENV{RT_DB_TYPE} or die "RT_DB_TYPE is not set\n";
my ( $host, $port ) = ( $ENV{RT_DB_HOST}, $ENV{RT_DB_PORT} );
my ( $dba, $dba_password ) = ( $ENV{RT_DBA_USER}, $ENV{RT_DBA_PASSWORD} );
my $driver = $type eq 'Pg' ? 'Pg' : 'mysql';
my $admin_db = $type eq 'Pg' ? 'dbname=postgres' : 'database=mysql';

sub connect_as {
    my ( $user, $password, $database ) = @_;
    return DBI->connect( "dbi:$driver:$database;host=$host;port=$port", $user, $password,
        { PrintError => 0, RaiseError => 0 } );
}

for my $attempt ( 1 .. 120 ) {
    last if connect_as( $dba, $dba_password, $admin_db );
    die "The database at $host:$port did not come up\n" if $attempt == 120;
    sleep 1;
}

my $rt = connect_as( 'rt_user', 'rt_pass', $type eq 'Pg' ? 'dbname=rt' : 'database=rt' );
my $initialised = $rt && $rt->selectrow_array('SELECT COUNT(*) FROM Users');
unless ($initialised) {
    system( '/opt/rt/sbin/rt-setup-database', '--action', 'init', '--dba', $dba, '--dba-password', $dba_password )
        == 0
        or die "rt-setup-database failed\n";
}

open my $ready, '>', '/tmp/rt-ready' or die "Cannot write /tmp/rt-ready: $!\n";
close $ready;
print "RT is ready\n";
sleep 3600 while 1;
